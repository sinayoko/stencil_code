from collections import namedtuple
import ctypes as ct
import numpy as np
from copy import deepcopy

from ctree.c.nodes import If, Lt, Constant, And, SymbolRef, Assign, Add, Mul, \
    Div, Mod, For, AddAssign, ArrayRef, FunctionCall, String, ArrayDef, Ref, \
    FunctionDecl, GtE, NotEq, Sub, Cast, Return, Array, BinaryOp, AugAssign, Or, Gt
from ctree.ocl.macros import get_global_id, get_local_id, get_local_size, \
    clSetKernelArg, NULL
from ctree.cpp.nodes import CppDefine
from ctree.ocl.nodes import OclFile
from ctree.templates.nodes import StringTemplate
from ctree.util import strides, product
import pycl as cl
from stencil_code.backend.local_size_computer import LocalSizeComputer

from stencil_code.stencil_exception import StencilException
from stencil_code.stencil_model import MathFunction
from stencil_code.backend.stencil_backend import StencilBackend
from stencil_code.backend.ocl_boundary_copier import boundary_kernel_factory


def kernel_dim_name(cur_dim):
    return "kernel_d{}".format(cur_dim)


def global_for_dim_name(cur_dim):
    return "global_size_d{}".format(cur_dim)


def local_for_dim_name(cur_dim):
    return "local_size_d{}".format(cur_dim)


def check_ocl_error(code_block, message="kernel"):
    return [
        Assign(
            SymbolRef("error_code"),
            code_block
        ),
        If(
            NotEq(SymbolRef("error_code"), SymbolRef("CL_SUCCESS")),
            [
                FunctionCall(
                    SymbolRef("printf"),
                    [
                        String("OPENCL ERROR: {}:error code \
                               %d\\n".format(message)),
                        SymbolRef("error_code")
                    ]
                ),
                Return(SymbolRef("error_code")),
            ]
        )
    ]

StencilArgConfig = namedtuple(
    'StencilArgConfig', ['size', 'dtype', 'ndim', 'shape']
)


class StencilOclTransformer(StencilBackend):
    def __init__(self, parent_lazy_specializer=None,
                 block_padding=None, arg_cfg=None, fusable_nodes=None,
                 testing=False):
        super(StencilOclTransformer, self).__init__(
            parent_lazy_specializer, arg_cfg, fusable_nodes, testing)
        self.block_padding = block_padding
        self.stencil_op = []
        self.load_mem_block = []
        self.local_block = None
        self.macro_defns = []
        self.project = None
        self.local_size = None
        self.global_size = None
        self.virtual_global_size = None
        self.boundary_kernels = None
        self.boundary_handlers = None
        self.loop_vars = {}
        self.output_grid = arg_cfg[0]
        # if self.parent_lazy_specializer.num_convolutions > 1:
        #     output_shape = (self.parent_lazy_specializer.num_convolutions,) + self.arg_cfg[0].shape
        #     output = np.zeros(output_shape).astype(self.arg_cfg[0].dtype)
        #     self.output_grid = (StencilArgConfig(len(output), output.dtype, output.ndim, output.shape),)
            # print self.output_grid[0].shape

    # noinspection PyPep8Naming
    def visit_Project(self, node):
        self.project = node
        node.files[0] = self.visit(node.files[0])
        return node

    # noinspection PyPep8Naming
    def visit_CFile(self, node):
        node.body = list(map(self.visit, node.body))
        node.config_target = 'opencl'
        node.body.insert(0, StringTemplate("""
            #ifdef __APPLE__
            #include <OpenCL/opencl.h>
            #else
            #include <CL/cl.h>
            #endif
            #include <stdio.h>
            """))
        return node

    # noinspection PyPep8Naming
    def visit_FunctionDecl(self, node):
        # This function grabs the input and output grid names which are used to
        self.local_block = SymbolRef.unique()
        # generate the proper array macros.
        arg_cfg = self.arg_cfg

        global_size = arg_cfg[0].shape

        if self.testing:
            local_size = (1, 1, 1)
        else:
            desired_device_number = -1
            device = cl.clGetDeviceIDs()[desired_device_number]
            lcs = LocalSizeComputer(global_size, device)
            local_size = lcs.compute_local_size_bulky()
            virtual_global_size = lcs.compute_virtual_global_size(local_size)
            self.global_size = global_size
            self.local_size = local_size
            self.virtual_global_size = virtual_global_size

        if self.parent_lazy_specializer.num_convolutions > 1:
            if len(self.arg_cfg) == len(node.params) - 1:
                # typically passed in arguments will not include output, in which case
                # it is coerced to be the same type as the first argument
                self.arg_cfg += (self.arg_cfg[0],)

            for index, arg in enumerate(self.arg_cfg):
                # fix up type of parameters, build a dictionary mapping param name to argument info
                param = node.params[index]
                param.type = np.ctypeslib.ndpointer(arg.dtype, arg.ndim, arg.shape)()
                self.input_dict[param.name] = arg
                self.input_names.append(param.name)
            self.output_grid_name = node.params[-1].name
            channel_kernels = []
            for c in range(3):
                channel_kernels.append(FunctionDecl(name="kernel_c{}".format(c),
                                                    params=node.params,
                                                    defn=[node.defn[c]]))
            for c in range(3):
                self.channel = c
                channel_kernels[c].defn = list(map(self.visit, channel_kernels[c].defn))[0]
                channel_kernels[c].name = "kernel_c{}".format(c)
                channel_kernels[c].set_kernel()

        else:
            self.function_decl_helper(node)  # this does the visiting

        for param in node.params:
            param.set_global()

        for param in node.params[:-1]:
            param.set_const()

        node.set_kernel()
        node.params[-1].set_global()
        node.params[-1].type = node.params[0].type
        node.params.append(SymbolRef(self.local_block.name, node.params[0].type))
        node.params[-1].set_local()
        if self.parent_lazy_specializer.num_convolutions == 1:
            node.defn = node.defn[0]

        # if boundary handling is copy we have to generate a collection of
        # boundary kernels to handle the on-gpu boundary copy
        if self.is_copied:
            device = cl.clGetDeviceIDs()[-1]
            self.boundary_handlers = boundary_kernel_factory(
                self.ghost_depth, self.output_grid,
                node.params[0].name,
                node.params[-2].name,  # second last parameter is output
                device
            )
            boundary_kernels = [
                FunctionDecl(
                    name=boundary_handler.kernel_name,
                    params=node.params,
                    defn=boundary_handler.generate_ocl_kernel_body(),
                )
                for boundary_handler in self.boundary_handlers
            ]

            self.project.files.append(OclFile('kernel', [node]))

            for dim, boundary_kernel in enumerate(boundary_kernels):
                boundary_kernel.set_kernel()
                self.project.files.append(OclFile(kernel_dim_name(dim),
                                                  [boundary_kernel]))

            self.boundary_kernels = boundary_kernels
        elif self.parent_lazy_specializer.num_convolutions > 1:
            for c in range(3):
                self.project.files.append(OclFile("kernel_c{}".format(c), [channel_kernels[c]]))
        else:
            self.project.files.append(OclFile('kernel', [node]))

        # print(self.project.files[0])
        # print(self.project.files[-1])
        # above this line is kernel creation, below is line is constructing stencil_control

        defn = [
            ArrayDef(
                SymbolRef('global', ct.c_ulong()), arg_cfg[0].ndim,
                Array(body=[Constant(d) for d in self.virtual_global_size])
            ),
            ArrayDef(
                SymbolRef('local', ct.c_ulong()), arg_cfg[0].ndim,
                Array(body=[Constant(s) for s in local_size])
                # [Constant(s) for s in [512, 512]]  # use this line to force a
                # opencl local size error
            ),
            Assign(SymbolRef("error_code", ct.c_int()), Constant(0)),
        ]
        setargs = [clSetKernelArg(
            SymbolRef('kernel'), Constant(d),
            FunctionCall(SymbolRef('sizeof'), [SymbolRef('cl_mem')]),
            Ref(SymbolRef('buf%d' % d))
        ) for d in range(len(arg_cfg) + 1)]
        #############
        if self.parent_lazy_specializer.num_convolutions > 1:
            setargs = []
            for c in range(3):
                setargs += [clSetKernelArg(
                    SymbolRef('kernel_c{}'.format(c)),
                    Constant(d),
                    FunctionCall(SymbolRef('sizeof'), [SymbolRef('cl_mem')]),
                    Ref(SymbolRef('buf%d' % d))
                ) for d in range(len(arg_cfg) + 1)]
        ####################
        from functools import reduce
        import operator
        local_mem_size = reduce(
            operator.mul,
            (size + 2 * self.parent_lazy_specializer.ghost_depth[index]
             for index, size in enumerate(local_size)),
            ct.sizeof(cl.cl_float())
        )
        if self.parent_lazy_specializer.num_convolutions > 1:
            for c in range(3):
                setargs.append(
                clSetKernelArg(
                    'kernel_c{}'.format(c), len(arg_cfg) + 1,
                    local_mem_size,
                    NULL()
                )
            )
        else:
            setargs.append(
                clSetKernelArg(
                    'kernel', len(arg_cfg) + 1,
                    local_mem_size,
                    NULL()
                )
            )

        defn.extend(setargs)
        finish_call = check_ocl_error(
            FunctionCall(SymbolRef('clFinish'), [SymbolRef('queue')]),
            "clFinish"
        )
        if self.parent_lazy_specializer.num_convolutions > 1:
            for c in range(3):
                enqueue_call = FunctionCall(SymbolRef('clEnqueueNDRangeKernel'), [
                    SymbolRef('queue'), SymbolRef('kernel_c{}'.format(c)),
                    Constant(self.parent_lazy_specializer.dim), NULL(),
                    SymbolRef('global'), SymbolRef('local'),
                    Constant(0), NULL(), NULL()
                ])
                defn.extend(check_ocl_error(enqueue_call, "clEnqueueNDRangeKernel"))
                defn.extend(finish_call)
        else:
            enqueue_call = FunctionCall(SymbolRef('clEnqueueNDRangeKernel'), [
                SymbolRef('queue'), SymbolRef('kernel'),
                Constant(self.parent_lazy_specializer.dim), NULL(),
                SymbolRef('global'), SymbolRef('local'),
                Constant(0), NULL(), NULL()
            ])

            defn.extend(check_ocl_error(enqueue_call, "clEnqueueNDRangeKernel"))

        params = [
            SymbolRef('queue', cl.cl_command_queue()),
            SymbolRef('kernel', cl.cl_kernel())
        ]
        if self.is_copied:
            for dim, boundary_kernel in enumerate(self.boundary_kernels):
                defn.extend([
                    ArrayDef(
                        SymbolRef(global_for_dim_name(dim), ct.c_ulong()),
                        arg_cfg[0].ndim,
                        Array(body=[Constant(d) for d in self.boundary_handlers[dim].global_size])
                    ),
                    ArrayDef(
                        SymbolRef(local_for_dim_name(dim), ct.c_ulong()),
                        arg_cfg[0].ndim,
                        Array(body=[Constant(s) for s in self.boundary_handlers[dim].local_size])
                    )
                ])
                setargs = [clSetKernelArg(
                    SymbolRef(kernel_dim_name(dim)), Constant(d),
                    FunctionCall(SymbolRef('sizeof'), [SymbolRef('cl_mem')]),
                    Ref(SymbolRef('buf%d' % d))
                ) for d in range(len(arg_cfg) + 1)]
                setargs.append(
                    clSetKernelArg(
                        SymbolRef(kernel_dim_name(dim)), len(arg_cfg) + 1,
                        local_mem_size,
                        NULL()
                    )
                )
                defn.extend(setargs)

                enqueue_call = FunctionCall(
                    SymbolRef('clEnqueueNDRangeKernel'), [
                        SymbolRef('queue'), SymbolRef(kernel_dim_name(dim)),
                        Constant(self.parent_lazy_specializer.dim), NULL(),
                        SymbolRef(global_for_dim_name(dim)),
                        SymbolRef(local_for_dim_name(dim)),
                        Constant(0), NULL(), NULL()
                    ]
                )
                defn.append(enqueue_call)

                params.extend([
                    SymbolRef(kernel_dim_name(dim), cl.cl_kernel())
                ])

        if self.parent_lazy_specializer.num_convolutions > 1:
            params = params[:-1]
            for c in range(3):
                params.extend([
                    SymbolRef('kernel_c{}'.format(c), cl.cl_kernel())
                ])

        # finish_call = check_ocl_error(
        #     FunctionCall(SymbolRef('clFinish'), [SymbolRef('queue')]),
        #     "clFinish"
        # )
        defn.extend(finish_call)
        defn.append(Return(SymbolRef("error_code")))

        params.extend(SymbolRef('buf%d' % d, cl.cl_mem())
                      for d in range(len(arg_cfg) + 1))

        control = FunctionDecl(ct.c_int32(), "stencil_control",
                               params=params,
                               defn=defn)

        return control  # FunctionDecl of stencil_control

    def global_array_macro(self, point):
        dim = len(self.input_grids[0].shape)
        index = point[dim - 1]
        for d in reversed(range(dim - 1)):
            index = Add(
                Mul(
                    index,
                    Constant(self.input_grids[0].shape[d])
                ),
                point[d]
            )

        return FunctionCall(SymbolRef("global_array_macro"), point)

    def gen_global_macro(self):
        index = "(d%d)" % (self.input_grids[0].ndim - 1)
        for x in reversed(range(self.input_grids[0].ndim - 1)):
            ndim = str(int(strides(self.input_grids[0].shape)[x]))
            index += "+((d%s) * %s)" % (str(x), ndim)
        return index

    def gen_ghost_global_macro(self):
        index = "(d%d)" % (self.input_grids[0].ndim - 1)
        for x in reversed(range(self.input_grids[0].ndim - 1)):
            ndim = str(int(strides(self.input_grids[0].shape)[x] + 2 * self.ghost_depth[x]))
            index += "+((d%s) * %s)" % (str(x), ndim)
        return index

    def local_array_macro(self, point):
        dim = len(self.input_grids[0].shape)
        index = get_local_id(dim)
        for d in reversed(range(dim)):
            index = Add(
                Mul(
                    index,
                    Add(
                        get_local_size(d),
                        Constant(2 * self.ghost_depth[d])
                    ),
                ),
                point[d]
            )
        return FunctionCall(SymbolRef("local_array_macro"), point)

    def gen_local_macro(self):
        dim = len(self.input_grids[0].shape)
        index = SymbolRef("d%d" % (dim - 1))
        for d in reversed(range(dim - 1)):
            base = Add(get_local_size(dim - 1),
                       Constant(2 * self.ghost_depth[dim - 1]))
            for s in range(d + 1, dim - 1):
                base = Mul(
                    base,
                    Add(get_local_size(s), Constant(2 * self.ghost_depth[s]))
                )
            index = Add(
                index, Mul(base, SymbolRef("d%d" % d))
            )
            index._force_parentheses = True
            index.right.right._force_parentheses = True
        return index

    def gen_global_index(self):
        dim = self.input_grids[0].ndim
        index = get_global_id(dim - 1)
        for d in reversed(range(dim - 1)):
            stride = strides(self.input_grids[0].shape)[d]
            index = Add(
                index,
                Mul(
                    get_global_id(d),
                    Constant(stride)
                )
            )
        return Mul(index, Constant(self.parent_lazy_specializer.num_convolutions))

    def load_shared_memory_block(self, target, ghost_depth):
        dim = len(self.input_grids[0].shape)
        body = []
        thread_id, num_threads, block_size = gen_decls(dim, ghost_depth)

        body.extend([Assign(SymbolRef("thread_id", ct.c_int()), thread_id),
                     Assign(SymbolRef("block_size", ct.c_int()), block_size),
                     Assign(SymbolRef("num_threads", ct.c_int()), num_threads)
                     ])
        base = None
        for i in reversed(range(0, dim - 1)):
            if base is not None:
                base = Mul(Add(get_local_size(i + 1),
                               Constant(self.ghost_depth[i + 1] * 2)),
                           base)
            else:
                base = Add(get_local_size(i + 1),
                           Constant(self.ghost_depth[i + 1] * 2))
        if base is not None:
            local_indices = [
                Assign(
                    SymbolRef("local_id%d" % (dim - 1), ct.c_int()),
                    Div(SymbolRef('tid'), base)
                ),
                Assign(
                    SymbolRef("r_%d" % (dim - 1), ct.c_int()),
                    Mod(SymbolRef('tid'), base)
                )
            ]
        else:
            local_indices = [
                Assign(
                    SymbolRef("local_id%d" % (dim - 1), ct.c_int()),
                    SymbolRef('tid')
                ),
                Assign(
                    SymbolRef("r_%d" % (dim - 1), ct.c_int()),
                    SymbolRef('tid')
                )
            ]
        for d in reversed(range(0, dim - 1)):
            base = None
            for i in reversed(range(d + 1, dim)):
                if base is not None:
                    base = Mul(
                        Add(get_local_size(i),
                            ghost_depth[i] * 2),
                        base
                    )
                else:
                    base = Add(get_local_size(i), Constant(ghost_depth[i] * 2))
            if base is not None and d != 0:
                local_indices.append(
                    Assign(
                        SymbolRef("local_id%d" % d, ct.c_int()),
                        Div(SymbolRef('r_%d' % (d + 1)), base)
                    )
                )
                local_indices.append(
                    Assign(
                        SymbolRef("r_%d" % d, ct.c_int()),
                        Mod(SymbolRef('r_%d' % (d + 1)), base)
                    )
                )
            else:
                local_indices.append(
                    Assign(
                        SymbolRef("local_id%d" % d, ct.c_int()),
                        SymbolRef('r_%d' % (d + 1))
                    )
                )
        input_array = 0 if self.parent_lazy_specializer.num_convolutions == 1 else self.channel
        if self.parent_lazy_specializer.num_convolutions == 1:
            body.append(
                For(
                    Assign(SymbolRef('tid', ct.c_int()), SymbolRef('thread_id')),
                    Lt(SymbolRef('tid'), SymbolRef('block_size')),
                    AddAssign(SymbolRef('tid'), SymbolRef('num_threads')),
                    local_indices + [Assign(
                        ArrayRef(
                            target,
                            SymbolRef('tid')
                        ),
                        ArrayRef(
                            SymbolRef(self.input_names[input_array]),
                            self.global_array_macro(
                                [FunctionCall(
                                    SymbolRef('clamp'),
                                    [Cast(ct.c_int(), Sub(Add(
                                        SymbolRef("local_id%d" % (dim - d - 1)),
                                        Mul(FunctionCall(
                                            SymbolRef('get_group_id'),
                                            [Constant(d)]),
                                            get_local_size(d))
                                    ), Constant(self.parent_lazy_specializer.ghost_depth[d]))),
                                        Constant(0), Constant(
                                            self.arg_cfg[0].shape[d]-1
                                        )
                                    ]
                                ) for d in range(0, dim)]
                            )
                        )
                    )]
                )
            )
        else:
            body.extend([Assign(SymbolRef("mem_local_id%d" % (d), ct.c_int()), Constant(0)) for d in range(dim)])
            body.extend([Assign(SymbolRef("r_%d" % (d + 1), ct.c_int()), Constant(0)) for d in range(dim - 1)])
            block_size = product(self.local_size[d] + (2 * self.ghost_depth[d]) for d in range(dim))
            num_threads = product(self.local_size)
            num_iterations = block_size / num_threads
            global_points = [Add(SymbolRef("mem_local_id%d" % (dim - d - 1)),
                                  Mul(FunctionCall(SymbolRef('get_group_id'), [Constant(d)]),get_local_size(d)))
                             for d in range(0, dim)]
            for iteration in range(num_iterations):
                base = None
                for i in reversed(range(0, dim - 1)):
                    if base is not None:
                        base = Mul(Add(get_local_size(i + 1),
                                       Constant(self.ghost_depth[i + 1] * 2)),
                                   base)
                    else:
                        base = Add(get_local_size(i + 1),
                                   Constant(self.ghost_depth[i + 1] * 2))
                if base is not None:
                    local_indices = [
                        Assign(
                            SymbolRef("mem_local_id%d" % (dim - 1)),
                            Div(Add(SymbolRef('thread_id'), Constant(iteration * num_threads)), base)
                        ),
                        Assign(
                            SymbolRef("r_%d" % (dim - 1)),
                            Mod(Add(SymbolRef('thread_id'), Constant(iteration * num_threads)), base)
                        )
                    ]
                else:
                    local_indices = [
                        Assign(
                            SymbolRef("mem_local_id%d" % (dim - 1)),
                            SymbolRef('tid')
                        ),
                        Assign(
                            SymbolRef("r_%d" % (dim - 1)),
                            Add(SymbolRef('thread_id'), Constant(iteration * num_threads))
                        )
                    ]
                for d in reversed(range(0, dim - 1)):
                    base = None
                    for i in reversed(range(d + 1, dim)):
                        if base is not None:
                            base = Mul(
                                Add(get_local_size(i),
                                    ghost_depth[i] * 2),
                                base
                            )
                        else:
                            base = Add(get_local_size(i), Constant(ghost_depth[i] * 2))
                    if base is not None and d != 0:
                        local_indices.append(
                            Assign(
                                SymbolRef("mem_local_id%d" % d),
                                Div(SymbolRef('r_%d' % (d + 1)), base)
                            )
                        )
                        local_indices.append(
                            Assign(
                                SymbolRef("r_%d" % d),
                                Mod(SymbolRef('r_%d' % (d + 1)), base)
                            )
                        )
                    else:
                        local_indices.append(
                            Assign(
                                SymbolRef("mem_local_id%d" % d),
                                SymbolRef('r_%d' % (d + 1))
                            )
                        )
                body.extend(local_indices)
                body.append(Assign(ArrayRef(target, Add(SymbolRef('thread_id'), Constant(iteration * num_threads))),
                                   ArrayRef(SymbolRef(self.input_names[input_array]),
                                            self.global_array_macro((global_points[0], global_points[1])))))

            body.append(Assign(SymbolRef("mem_local_id1"),
                               Div(Sub(Sub(SymbolRef("block_size"), SymbolRef("thread_id")), Constant(1)),
                                   Add(get_local_size(1), Constant(2 * self.ghost_depth[1])))))
            body.append(Assign(SymbolRef("mem_local_id0"),
                               Mod(Sub(Sub(SymbolRef("block_size"), SymbolRef("thread_id")), Constant(1)),
                                   Add(get_local_size(1), Constant(2 * self.ghost_depth[1])))))
            body.append(Assign(ArrayRef(target, Sub(Sub(SymbolRef("block_size"), SymbolRef("thread_id")), Constant(1))),
                               ArrayRef(SymbolRef(self.input_names[input_array]),
                                        self.global_array_macro((global_points[0], global_points[1])))))
        return body

    def visit_SymbolRef(self, node):
        if node.name in self.loop_vars:
            return self.loop_vars[node.name]
        else:
            return super(StencilOclTransformer, self).visit_SymbolRef(node)

    # noinspection PyPep8Naming
    def visit_InteriorPointsLoop(self, node):
        dim = len(self.input_grids[0].shape)
        self.kernel_target = node.target
        condition = And(
            Lt(get_global_id(0),
               Constant(self.arg_cfg[0].shape[0] - self.ghost_depth[0])),
            GtE(get_global_id(0),
                Constant(self.ghost_depth[0]))
        )
        for d in range(1, len(self.arg_cfg[0].shape)):
            condition = And(
                condition,
                And(
                    Lt(get_global_id(d),
                       Constant(self.arg_cfg[0].shape[d] -
                                self.ghost_depth[d])),
                    GtE(get_global_id(d),
                        Constant(self.ghost_depth[d]))
                )
            )
        body = []

        if self.parent_lazy_specializer.num_convolutions > 1:
            self.macro_defns = [
                CppDefine("local_array_macro", ["d%d" % i for i in range(dim)],
                          self.gen_local_macro()),
                CppDefine("global_array_macro", ["d%d" % i for i in range(dim)],
                          self.gen_ghost_global_macro())
            ]
        else:
            self.macro_defns = [
                CppDefine("local_array_macro", ["d%d" % i for i in range(dim)],
                          self.gen_local_macro()),
                CppDefine("global_array_macro", ["d%d" % i for i in range(dim)],
                          self.gen_global_macro())
            ]
        body.extend(self.macro_defns)

        global_idx = 'global_index'
        self.output_index = global_idx
        body.append(Assign(SymbolRef('global_index', ct.c_int()),
                    self.gen_global_index()))

        self.load_mem_block = self.load_shared_memory_block(
            self.local_block, self.ghost_depth)
        body.extend(self.load_mem_block)
        body.append(FunctionCall(SymbolRef("barrier"),
                                 [SymbolRef("CLK_LOCAL_MEM_FENCE")]))
        if self.parent_lazy_specializer.num_convolutions > 1:
            self.var_list = []
        for d in range(0, dim):
            body.append(Assign(SymbolRef('local_id%d' % d, ct.c_int()),
                               Add(get_local_id(d),
                                   Constant(self.ghost_depth[d]))))
            self.var_list.append("local_id%d" % d)

        self.index_target_dict[node.target] = global_idx

        for child in map(self.visit, node.body):
            if self.parent_lazy_specializer.num_convolutions > 1:
                self.stencil_op = []
            if isinstance(child, list):
                self.stencil_op.extend(child)
            else:
                self.stencil_op.append(child)

        conditional = None
        for dim in range(len(self.input_grids[0].shape)):
            if self.virtual_global_size[dim] != self.global_size[dim]:
                if conditional is None:
                    conditional = Lt(get_global_id(dim),
                                     Constant(self.global_size[dim]))
                else:
                    conditional = And(conditional,
                                      Lt(get_global_id(dim),
                                         Constant(self.global_size[dim])))

        if conditional is not None:
            body.append(If(conditional, self.stencil_op))
        else:
            body.extend(self.stencil_op)

        return body

    # noinspection PyPep8Naming
    def visit_NeighborPointsLoop(self, node):
        """
        unrolls the neighbor points loop, appending each current block of the body to a new
        body for each neighbor point, a side effect of this is local python functions of the
        neighbor point can be collapsed out, for example, a custom python distance function based
        on neighbor distance can be resolved at transform time
        DANGER: this blows up on large neighborhoods
        :param node:
        :return:
        """
        # TODO: unrolling blows up when neighborhood size is large.
        neighbors_id = node.neighbor_id
        # grid_name = node.grid_name
        # grid = self.input_dict[grid_name]
        zero_point = tuple([0 for x in range(self.parent_lazy_specializer.dim)])
        self.neighbor_target = node.neighbor_target
        # self.neighbor_grid_name = grid_name

        # self.index_target_dict[node.neighbor_target] = self.index_target_dict[node.reference_point]
        body = []
        for x in self.parent_lazy_specializer.neighbors(zero_point, neighbors_id):
            # TODO: add line below to manage indices that refer to neighbor points loop
            # self.var_list.append(node.neighbor_target)
            self.offset_list = list(x)
            self.offset_dict[self.neighbor_target] = list(x)
            for statement in node.body:
                body.append(self.visit(deepcopy(statement)))
                # body.append(StringTemplate('printf("acc = %d\\n", acc);'))
        self.neighbor_target = None
        return body

    # noinspection PyPep8Naming
    def visit_MultiPointsLoop(self, node):
        """
        unrolls the multipoints loop, intended to apply multiple convolution matrices
        with respect to one point in the input grid
        :param node:
        :return:
        """
        zero_point = tuple([0 for _ in range(self.parent_lazy_specializer.dim)])
        self.input_target = node.input_target
        self.output_target = node.output_target
        self.coefficient = node.coefficient

        body = []
        body.append(Assign(SymbolRef("neighbor", ct.c_float()), Constant(0)))
        neighbor_num = 0
        for x in self.parent_lazy_specializer.neighbors(zero_point, 0):
            for conv_id in range(self.parent_lazy_specializer.num_convolutions):
                self.offset_list = list(x)
                self.offset_dict[self.input_target] = list(x)
                self.index_target_dict[self.output_target] = Add(SymbolRef('global_index'), Constant(conv_id))
                self.loop_vars[self.coefficient] = \
                    Constant(self.parent_lazy_specializer.coefficients[(conv_id, self.channel, neighbor_num)])
                for statement in node.body:
                    statement = self.visit(deepcopy(statement))
                    if conv_id == 0:
                        body.append(Assign(SymbolRef("neighbor"), statement.value.left))
                    statement.value.left = SymbolRef("neighbor")
                    body.append(statement)
            neighbor_num += 1
        self.neighbor_target = None
        return body

    # noinspection PyPep8Naming
    def visit_GridElement(self, node):
        """
        handles code generation for array references.
        if a reference to the interior points loop index variable is found replace it with
        the global_index
        if a reference to the neighbor points index variable is found and the grid_name we are working
        on is the first parameter then reference the local memory block, this is because the local
        memory block is the current work_group elements mapped into a larger space that includes the
        ghost zone

        :param node:
        :return:
        """
        grid_name = node.grid_name
        target = node.target

        if isinstance(target, SymbolRef):
            target_name = target.name
            if target_name in self.index_target_dict:
                return ArrayRef(SymbolRef(grid_name), SymbolRef(self.index_target_dict[target_name]))

            elif target_name in self.offset_dict:
                if node.grid_name == self.input_names[0] or (self.parent_lazy_specializer.num_convolutions > 1 and (node.grid_name == self.input_names[1] or node.grid_name == self.input_names[2])):
                    pt = list(map(lambda x, y: Add(SymbolRef(x), Constant(y)),
                                  self.var_list, self.offset_list))
                    index = self.local_array_macro(pt)
                    return ArrayRef(self.local_block, index)
                else:
                    raise StencilException(
                        "{}[{}] neighbor index does not reference first input {}".format(
                            node.grid_name, target_name, self.input_names[0]
                        )
                    )
            else:
                return ArrayRef(SymbolRef(grid_name), target)

        elif isinstance(target, FunctionCall) or \
                isinstance(target, MathFunction) or \
                isinstance(target, BinaryOp):
            return ArrayRef(SymbolRef(grid_name), self.visit(target))

        raise StencilException(
            "Unsupported GridElement encountered: {} type {} \
                {}".format(grid_name, type(target), repr(target)))


def gen_decls(dim, ghost_depth):
    thread_id = get_local_id(dim - 1)
    num_threads = get_local_size(dim - 1)
    block_size = Add(
        get_local_size(dim - 1),
        Constant(ghost_depth[dim - 1] * 2)
    )
    for d in reversed(range(0, dim - 1)):
        base = get_local_size(dim - 1)
        for s in range(d, dim - 2):
            base = Mul(get_local_size(s + 1), base)

        thread_id = Add(
            Mul(get_local_id(d), base),
            thread_id
        )
        num_threads = Mul(get_local_size(d), num_threads)
        block_size = Mul(
            Add(get_local_size(d), Constant(ghost_depth[d] * 2)),
            block_size
        )
    return thread_id, num_threads, block_size