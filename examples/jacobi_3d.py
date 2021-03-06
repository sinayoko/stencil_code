from __future__ import print_function
from stencil_code.stencil_kernel import StencilKernel
import numpy as np

import logging
logging.basicConfig(level=0)

__author__ = 'chick'


class Jacobi3D(StencilKernel):

    def __init__(self, alpha, beta):
        super(Jacobi3D, self).__init__(backend='c')
        self.alpha = alpha
        self.beta = beta

    def kernel(self, in_grid, out_grid):
        for x in in_grid.interior_points():
            out_grid[x] = self.alpha * in_grid[x]
            for y in in_grid.neighbor_points(x, 1):
                out_grid += self.beta * in_grid[y]


if __name__ == '__main__':
    j = Jacobi3D(1.0, 0.25)
    print("j.alpha = {}".format(j.alpha))