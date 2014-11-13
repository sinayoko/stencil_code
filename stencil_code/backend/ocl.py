from ctree.c.nodes import Lt, Constant, And, SymbolRef, Assign, Add, Mul, \
    Div, Mod, For, AddAssign, ArrayRef, FunctionCall, ArrayDef, Ref, \
    FunctionDecl, GtE, Sub, Cast
from ctree.ocl.macros import get_global_id, get_local_id, get_local_size, \
    clSetKernelArg, NULL, barrier, CLK_LOCAL_MEM_FENCE
from ctree.cpp.nodes import CppDefine
from ctree.ocl.nodes import OclFile
from ctree.templates.nodes import StringTemplate
from hindemith.fusion.core import KernelCall
from stencil_code.stencil_exception import StencilException
from ..stencil_model import MathFunction, OclNeighborLoop, MacroDefns, \
    LoadSharedMemBlock
from .stencil_backend import StencilBackend
import ctypes as ct
import pycl as cl


class StencilOclSemanticTransformer(StencilBackend):
    def __init__(self, input_grids=None, output_grid=None, kernel=None,
                 fusion_padding=None):
        super(StencilOclSemanticTransformer, self).__init__(
            input_grids, output_grid, kernel)
        self.fusion_padding = fusion_padding

    def visit_FunctionDecl(self, node):
        # This function grabs the input and output grid names which are used to
        # generate the proper array macros.
        super(StencilOclSemanticTransformer, self).visit_FunctionDecl(node)
        for index, param in enumerate(node.params[:-1]):
            # TODO: Transform numpy type to ctype
            param.type = ct.POINTER(ct.c_float)()
            param.set_global()
            param.set_const()
        node.set_kernel()
        node.params[-1].set_global()
        node.params[-1].type = ct.POINTER(ct.c_float)()
        node.params.append(SymbolRef('block', ct.POINTER(ct.c_float)()))
        node.params[-1].set_local()
        node.defn = node.defn[0]
        return node

    def local_array_macro(self, point):
        dim = len(self.output_grid.shape)
        index = get_local_id(dim)
        for d in reversed(range(dim)):
            index = Add(
                Mul(
                    index,
                    Add(
                        get_local_size(d),
                        Constant(2 * self.ghost_depth)
                    ),
                ),
                point[d]
            )
        return FunctionCall(SymbolRef("local_array_macro"), point)

    def global_array_macro(self, point):
        dim = len(self.output_grid.shape)
        index = point[dim - 1]
        for d in reversed(range(dim - 1)):
            index = Add(
                Mul(
                    index,
                    Constant(self.output_grid.shape[d])
                ),
                point[d]
            )

        return FunctionCall(SymbolRef("global_array_macro"), point)

    def gen_global_macro(self):
        dim = len(self.output_grid.shape)
        index = "d%d" % (dim - 1)
        for d in reversed(range(dim - 1)):
            index = "(" + index + ") * %d" % self.output_grid.shape[d]
            index += " + d%d" % d
        return index

    def gen_local_macro(self):
        dim = len(self.output_grid.shape)
        index = "d%d" % (dim - 1)
        for d in reversed(range(dim - 1)):
            index = "(" + index + ") * (get_local_size(%d) + %d)" % (
                d, 2 * self.ghost_depth * self.fusion_padding)
            index += " + d%d" % d
        return index

    def load_shared_memory_block(self, target, padding):
        dim = len(self.output_grid.shape)
        decls = []
        thread_id = get_local_id(0)
        num_threads = get_local_size(0)
        block_size = Add(get_local_size(0), padding)
        for d in range(1, dim):
            thread_id = Add(
                Mul(get_local_id(d), get_local_size(d - 1)),
                thread_id
            )
            num_threads = Mul(get_local_size(d), num_threads)
            block_size = Mul(
                Add(get_local_size(d), padding),
                block_size
            )

        decls.append(Assign(SymbolRef("thread_id", ct.c_int()), thread_id))
        decls.append(Assign(SymbolRef("block_size", ct.c_int()), block_size))
        decls.append(Assign(SymbolRef("num_threads", ct.c_int()), num_threads))
        base = None
        for i in reversed(range(0, dim - 1)):
            if base is not None:
                base = Mul(Add(get_local_size(i), padding), base)
            else:
                base = Add(get_local_size(i), padding)
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
        for d in reversed(range(0, dim - 1)):
            base = None
            for i in reversed(range(0, d - 1)):
                if base is not None:
                    base = Mul(Add(get_local_size(i), padding), base)
                else:
                    base = Add(get_local_size(i), padding)
            if base is not None:
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
        body = For(
            Assign(SymbolRef('tid', ct.c_int()), SymbolRef('thread_id')),
            Lt(SymbolRef('tid'), SymbolRef('block_size')),
            AddAssign(SymbolRef('tid'), SymbolRef('num_threads')),
            local_indices + [
                Assign(
                    ArrayRef(target, SymbolRef('tid')),
                    ArrayRef(
                        SymbolRef(self.input_names[0]),
                        self.global_array_macro(
                            [Add(
                                SymbolRef("local_id%d" % d),
                                Mul(FunctionCall(SymbolRef('get_group_id'),
                                                 [Constant(d)]),
                                    get_local_size(d))
                            ) for d in range(0, dim)]
                        )
                    )
                )
            ]
        )
        return decls, [body, barrier(CLK_LOCAL_MEM_FENCE())]

    def visit_InteriorPointsLoop(self, node):
        dim = len(self.output_grid.shape)
        self.kernel_target = node.target
        body = [
            MacroDefns([
                CppDefine("local_array_macro", ["d%d" % i for i in range(dim)],
                          self.gen_local_macro()),
                CppDefine("global_array_macro", ["d%d" % i for i in range(dim)],
                          self.gen_global_macro())
            ]),
            LoadSharedMemBlock(*self.load_shared_memory_block(
                SymbolRef('block'),
                Constant(self.ghost_depth * 2 * self.fusion_padding))
            )
        ]

        self.output_index = SymbolRef('global_index')
        next_body = []
        for d in range(0, dim):
            next_body.append(Assign(SymbolRef('local_id%d' % d, ct.c_int()),
                             Add(get_local_id(d), Constant(self.ghost_depth))))
            self.var_list.append("local_id%d" % d)
        map(next_body.extend, map(self.visit, node.body))
        body.append(OclNeighborLoop(next_body, self.output_grid.shape,
                                    self.ghost_depth))
        return body

    # Handle array references
    def visit_GridElement(self, node):
        grid_name = node.grid_name
        target = node.target
        if isinstance(target, SymbolRef):
            target = target.name
            if target == self.kernel_target:
                if grid_name is self.output_grid_name:
                    return ArrayRef(SymbolRef(self.output_grid_name),
                                    SymbolRef(self.output_index))
                elif grid_name in self.input_dict:
                    # grid = self.input_dict[grid_name]
                    pt = list(map(lambda x: SymbolRef(x), self.var_list))
                    # index = self.gen_array_macro(grid_name, pt)
                    index = self.local_array_macro(pt)
                    return ArrayRef(SymbolRef('block'), index)
            elif grid_name == self.neighbor_grid_name:
                pt = list(map(lambda x, y: Add(SymbolRef(x), SymbolRef(y)),
                              self.var_list, self.offset_list))
                # index = self.gen_array_macro(grid_name, pt)
                index = self.local_array_macro(pt)
                # index = SymbolRef('out_index')
                return ArrayRef(SymbolRef('block'), index)
        elif isinstance(target, FunctionCall) or \
                isinstance(target, MathFunction):
            return ArrayRef(SymbolRef(grid_name), self.visit(target))
        return node


class StencilOclTransformer(StencilBackend):
    def __init__(self, input_grids=None, output_grid=None, kernel=None,
                 block_padding=None, arg_cfg=None, fusable_nodes=None,
                 testing=False):
        super(StencilOclTransformer, self).__init__(
            input_grids, output_grid, kernel, arg_cfg, fusable_nodes, testing)
        self.block_padding = block_padding
        self.stencil_op = []
        self.load_mem_block = []
        self.macro_defns = []

    def visit_Project(self, node):
        self.project = node
        node.files[0] = self.visit(node.files[0])
        return node

    def visit_CFile(self, node):
        node.body = list(map(self.visit, node.body))
        node.body.insert(0, StringTemplate("""
            #ifdef __APPLE__
            #include <OpenCL/opencl.h>
            #else
            #include <CL/cl.h>
            #endif
            """))
        return node

    def visit_FunctionDecl(self, node):
        # This function grabs the input and output grid names which are used to
        # generate the proper array macros.
        super(StencilOclTransformer, self).visit_FunctionDecl(node)
        for index, param in enumerate(node.params[:-1]):
            # TODO: Transform numpy type to ctype
            param.type = ct.POINTER(ct.c_float)()
            param.set_global()
            param.set_const()
        node.set_kernel()
        node.params[-1].set_global()
        node.params[-1].type = ct.POINTER(ct.c_float)()
        node.params.append(SymbolRef('block', ct.POINTER(ct.c_float)()))
        node.params[-1].set_local()
        node.defn = node.defn[0]
        self.project.files.append(OclFile('kernel', [node]))
        arg_cfg = self.arg_cfg
        if self.testing:
            local_size = (1, 1, 1)
        else:
            devices = cl.clGetDeviceIDs()
            max_sizes = cl.clGetDeviceInfo(
                devices[-1], cl.cl_device_info.CL_DEVICE_MAX_WORK_ITEM_SIZES)
            max_total = cl.clGetDeviceInfo(
                devices[-1], cl.cl_device_info.CL_DEVICE_MAX_WORK_GROUP_SIZE)

            if len(arg_cfg[0].shape) == 3:
                x_len, y_len, z_len = 1, 1, 1
                while True:
                    if arg_cfg[0].shape[0] % 2 == 1:
                        x_len = 1
                    else:
                        x_len = min(max_sizes[0], x_len * 2)
                    if max_total - z_len * x_len * y_len <= 0:
                        break
                    if arg_cfg[0].shape[1] % 2 == 1:
                        y_len = 1
                    else:
                        y_len = min(max_sizes[1], y_len * 2)
                    if max_total - z_len * x_len * y_len <= 0:
                        break
                    if arg_cfg[0].shape[2] % 2 == 1:
                        z_len = 1
                    else:
                        z_len = min(max_sizes[2], z_len * 2)
                    if max_total - z_len * x_len * y_len <= 0:
                        break
                    if x_len == arg_cfg[0].shape[0] or \
                            y_len == arg_cfg[0].shape[1] or \
                            z_len == arg_cfg[0].shape[2]:
                        break

                local_size = (x_len, y_len, z_len)
            elif len(arg_cfg[0].shape) == 2:
                x_len, y_len = 1, 1
                while True:
                    if arg_cfg[0].shape[0] % 2 == 1:
                        x_len = 1
                    else:
                        x_len = min(max_sizes[0], x_len * 2)
                    if max_total - x_len * y_len <= 0:
                        break
                    if arg_cfg[0].shape[1] % 2 == 1:
                        y_len = 1
                    else:
                        y_len = min(max_sizes[1], y_len * 2)
                    if max_total - x_len * y_len <= 0:
                        break
                    if x_len == arg_cfg[0].shape[0] or \
                            y_len == arg_cfg[0].shape[1]:
                        break

                local_size = (x_len, y_len)
                if arg_cfg[0].shape[0] % x_len != 0 or \
                   arg_cfg[0].shape[1] % y_len != 0:
                    raise StencilException(
                        'opencl backend must have sizes that are multiples of {}'.format(
                            local_size
                        ))
            else:
                local_size = (min(
                    max_total, max_sizes[0], arg_cfg[0].shape[0] / 2))

        defn = [
            ArrayDef(
                SymbolRef('global', ct.c_ulong()), arg_cfg[0].ndim,
                [Constant(d)
                 for d in arg_cfg[0].shape]
            ),
            ArrayDef(
                SymbolRef('local', ct.c_ulong()), arg_cfg[0].ndim,
                [Constant(s) for s in local_size]
            )
        ]
        setargs = [clSetKernelArg(
            SymbolRef('kernel'), Constant(d),
            FunctionCall(SymbolRef('sizeof'), [SymbolRef('cl_mem')]),
            Ref(SymbolRef('buf%d' % d))
        ) for d in range(len(arg_cfg) + 1)]
        from functools import reduce
        import operator
        local_mem_size = reduce(
            operator.mul,
            (size + 2 * self.kernel.ghost_depth[index]
             for index, size in enumerate(local_size)),
            ct.sizeof(cl.cl_float())
        )
        setargs.append(
            clSetKernelArg(
                'kernel', len(arg_cfg) + 1,
                local_mem_size,
                NULL()
            )
        )
        defn.extend(setargs)
        enqueue_call = FunctionCall(SymbolRef('clEnqueueNDRangeKernel'), [
            SymbolRef('queue'), SymbolRef('kernel'),
            Constant(self.kernel.dim), NULL(),
            SymbolRef('global'), SymbolRef('local'),
            Constant(0), NULL(), NULL()
        ])
        finish_call = FunctionCall(SymbolRef('clFinish'), [SymbolRef('queue')])
        defn.extend((enqueue_call, finish_call))
        params = [
            SymbolRef('queue', cl.cl_command_queue()),
            SymbolRef('kernel', cl.cl_kernel())
        ]
        params.extend(SymbolRef('buf%d' % d, cl.cl_mem())
                      for d in range(len(arg_cfg) + 1))

        control = FunctionDecl(None, "stencil_control",
                               params=params,
                               defn=defn)

        self.fusable_nodes.append(KernelCall(
            control, node, arg_cfg[0].shape,
            defn[0], tuple(local_size[i] for i in range(arg_cfg[0].ndim)),
            defn[1], enqueue_call, finish_call, setargs, self.load_mem_block,
            self.stencil_op, self.macro_defns, self.kernel.ghost_depth
        ))
        return control

    def global_array_macro(self, point):
        dim = len(self.output_grid.shape)
        index = point[dim - 1]
        for d in reversed(range(dim - 1)):
            index = Add(
                Mul(
                    index,
                    Constant(self.output_grid.shape[d])
                ),
                point[d]
            )

        return FunctionCall(SymbolRef("global_array_macro"), point)

    def gen_global_macro(self):
        index = "(d%d)" % (self.output_grid.ndim - 1)
        for x in reversed(range(self.output_grid.ndim - 1)):
            ndim = str(int(self.output_grid.strides[x] /
                           self.output_grid.itemsize))
            index += "+((d%s) * %s)" % (str(x), ndim)
        return index

    def local_array_macro(self, point):
        dim = len(self.output_grid.shape)
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

    def gen_array_macro(self, arg, point):
        dim = len(self.output_grid.shape)
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
        dim = len(self.output_grid.shape)
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
        dim = self.output_grid.ndim
        index = get_global_id(dim - 1)
        for d in reversed(range(dim - 1)):
            stride = self.output_grid.strides[d] // \
                self.output_grid.itemsize
            index = Add(
                index,
                Mul(
                    get_global_id(d),
                    Constant(stride)
                )
            )
        return index

    def load_shared_memory_block(self, target, ghost_depth):
        dim = len(self.output_grid.shape)
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
                        SymbolRef(self.input_names[0]),
                        self.global_array_macro(
                            [FunctionCall(
                                SymbolRef('clamp'),
                                [Cast(ct.c_int(), Sub(Add(
                                    SymbolRef("local_id%d" % (dim - d - 1)),
                                    Mul(FunctionCall(
                                        SymbolRef('get_group_id'),
                                        [Constant(d)]),
                                        get_local_size(d))
                                ), Constant(self.kernel.ghost_depth[d]))),
                                    Constant(0), Constant(
                                        self.arg_cfg[0].shape[d] -
                                        self.kernel.ghost_depth[d])
                                ]
                            ) for d in range(0, dim)]
                        )
                    )
                )]
            )
        )
        return body

    def visit_InteriorPointsLoop(self, node):
        dim = len(self.output_grid.shape)
        self.kernel_target = node.target
        cond = And(
            Lt(get_global_id(0),
               Constant(self.arg_cfg[0].shape[0] - self.ghost_depth[0])),
            GtE(get_global_id(0),
                Constant(self.ghost_depth[0]))
        )
        for d in range(1, len(self.arg_cfg[0].shape)):
            cond = And(
                cond,
                And(
                    Lt(get_global_id(d),
                       Constant(self.arg_cfg[0].shape[d] - self.ghost_depth[d])),
                    GtE(get_global_id(d),
                        Constant(self.ghost_depth[d]))
                )
            )
        body = []

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
            SymbolRef('block'), self.ghost_depth)
        body.extend(self.load_mem_block)
        body.append(FunctionCall(SymbolRef("barrier"),
                                 [SymbolRef("CLK_LOCAL_MEM_FENCE")]))
        for d in range(0, dim):
            body.append(Assign(SymbolRef('local_id%d' % d, ct.c_int()),
                               Add(get_local_id(d),
                                   Constant(self.ghost_depth[d]))))
            self.var_list.append("local_id%d" % d)

        for child in map(self.visit, node.body):
            if isinstance(child, list):
                self.stencil_op.extend(child)
            else:
                self.stencil_op.append(child)
        body.extend(self.stencil_op)
        return body

    # Handle array references
    def visit_GridElement(self, node):
        grid_name = node.grid_name
        target = node.target
        if isinstance(target, SymbolRef):
            target = target.name
            if target == self.kernel_target:
                if grid_name is self.output_grid_name:
                    return ArrayRef(SymbolRef(self.output_grid_name),
                                    SymbolRef(self.output_index))
                elif grid_name in self.input_dict:
                    # grid = self.input_dict[grid_name]
                    pt = list(map(lambda x: SymbolRef(x), self.var_list))
                    # index = self.gen_array_macro(grid_name, pt)
                    index = self.local_array_macro(pt)
                    return ArrayRef(SymbolRef('block'), index)
            else:
                pt = list(map(lambda x, y: Add(SymbolRef(x), Constant(y)),
                              self.var_list, self.offset_list))
                # index = self.gen_array_macro(grid_name, pt)
                index = self.local_array_macro(pt)
                # index = SymbolRef('out_index')
                return ArrayRef(SymbolRef('block'), index)
        elif isinstance(target, FunctionCall) or \
                isinstance(target, MathFunction):
            return ArrayRef(SymbolRef(grid_name), self.visit(target))
        raise Exception(
            "Unsupported GridElement encountered: {0}".format(grid_name))


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
