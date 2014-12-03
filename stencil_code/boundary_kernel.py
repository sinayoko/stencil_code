from __future__ import print_function
import numpy
import numpy.random
from stencil_code.stencil_exception import StencilException
from stencil_code.backend.ocl_tools import product, OclTools

__author__ = 'chick'

from ctree.c.nodes import Lt, Constant, And, SymbolRef, Assign, Add, Mul, \
    Div, Mod, For, AddAssign, ArrayRef, FunctionCall, ArrayDef, Ref, \
    FunctionDecl, GtE, Sub, Cast
from hindemith.fusion.core import KernelCall


class BoundaryCopyKernel(object):
    """
    container for numbers necessary to generate and to call a kernel
    that copies boundary points from input to output during stencil
    copy boundary handling option
    """
    def __init__(self, halo, grid, dimension, device=None):
        """

        :param halo: the halo shape
        :param grid: the shape of the grid that stencil is to be applied to
        :param dimension: the dimension this kernel applies to
        :return:
        """
        self.halo = tuple(halo)
        self.grid = grid
        self.shape = grid.shape

        # check for some pathologies and raise exception if any are present
        if len(halo) != len(self.shape):
            raise StencilException("halo {} can't apply to grid shape {}".format(self.halo, self.shape))
        # halo or grid to small
        if any([x < 1 or y < 1 for x, y in zip(self.halo, self.shape)]):
            raise StencilException(
                "halo {} can't be bigger than grid {} in any dimension".format(self.halo, self.shape)
            )
        # no interior points in a dimension
        if any([s <= 2*h for h, s in zip(self.halo, self.shape)]):
            raise StencilException("halo {} can't span grid shape {} in any dimension".format(self.halo, self.shape))

        self.dimension = dimension
        self.dimensions = len(grid.shape)

        self.device = device

        self.global_size, self.global_offset = self.compute_global_size()
        self.local_size = OclTools(device=None).compute_local_size(self.global_size)

        self.kernel_name = "boundary_copy_kernel_{}d_dim_{}".format(len(halo), dimension)

    def compute_global_size(self):
        dimension_sizes = [x for x in self.shape]
        dimension_offsets = [0 for _ in self.shape]
        for other_dimension in range(self.dimension):
            dimension_sizes[other_dimension] -= max(1, (2 * self.halo[other_dimension]))
            dimension_offsets.append(self.halo[other_dimension])
        dimension_sizes[self.dimension] = self.halo[self.dimension]
        return dimension_sizes, dimension_offsets


def boundary_kernel_factory(halo, grid, device=None):
    return [
        BoundaryCopyKernel(halo, grid, dimension, device)
        for dimension in range(len(grid.shape))
    ]


if __name__ == '__main__':
    import itertools

    numpy.random.seed(0)

    for dims in range(1, 4):
        shape_list = [8, 1014, 4096][:dims]
        for _ in range(dims):
            shape = numpy.random.random(shape_list).astype(numpy.int) + 1

            print("Dimension {} {}".format(dims, "="*80))
            for shape in itertools.permutations(shape_list):
                rand_shape = map(lambda x: numpy.random.randint(5, x), shape)
                in_grid = numpy.zeros(rand_shape)
                halo = map(lambda x: numpy.random.randint(1, (max(2, (x-1)/2))), rand_shape)

                print("shape {} halo {}".format(rand_shape, halo))
                boundary_kernels = boundary_kernel_factory(halo, in_grid)

                for dim, bk0 in enumerate(boundary_kernels):
                    print("dim {} {:16} {:16} ".format(
                        dim, in_grid.shape, halo
                    ), end="")
                    print("global {:16} local {:16}".format(
                        bk0.global_size, bk0.local_size
                    ))

