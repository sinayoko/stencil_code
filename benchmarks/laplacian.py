from scipy.ndimage import convolve
from scipy.ndimage.filters import laplace
import numpy as np
from stencil_code.stencil_grid import StencilGrid
from stencil_code.stencil_kernel import StencilKernel
from stencil_code.stencil_kernel2 import StencilKernel2
from ctree.util import Timer

width = 2**11 + 2
height = width

image = np.random.rand(width, height)
# print("Print numpy image: ", image)
stencil = np.array([[0, 1, 0],[1, -4, 1], [0, 1, 0]])

#
# old
#
class Kernel(StencilKernel):
    def kernel(self, in_grid, out_grid):
        for x in in_grid.interior_points():
            out_grid[x] = -4 * in_grid[x]
            for y in in_grid.neighbors(x, 1):
                out_grid[x] += in_grid[y]

#
# new
#
class LaplacianFilter(StencilKernel2):
    def kernel(self, input):
        output = np.empty_like(input)
        for x in self.output_points(output):
            for neighbor_point, coefficient_index in self.neighbor_coff(x):
                output[x] += self.coefficients[coefficient_index] * input[neighbor_point]

laplacian_coefficients = [
    [0, 1, 0],
    [1, -4, 1],
    [0, 1, 0],
]
stencil = LaplacianFilter(coefficient_definition=laplacian_coefficients)

in_grid = StencilGrid([width, height])
for i in range(width):
    for j in range(height):
        in_grid[(i, j)] = image[(i, j)]

# print("StencilGrid in_grid:", in_grid)
iterations = 10
total = 0.0
for _ in range(iterations):
    with Timer() as t:
        out_image = convolve(image, stencil, mode='constant', cval=0.0)
    total += t.interval
print("Numpy convolve avg: {0}".format(total/iterations))

total = 0.0
for _ in range(iterations):
    with Timer() as t:
        out_image2 = laplace(image, mode='constant', cval=0.0)
    total += t.interval
print("Numpy laplace avg: {0}".format(total/iterations))

np.testing.assert_array_almost_equal(out_image, out_image2)

out_grid = StencilGrid([width, height])

total = 0.0
for _ in range(iterations):
    with Timer() as t:
        Kernel(backend='ocl').kernel(in_grid, out_grid)
    total += t.interval
print("Specialized with compile time avg: {0}".format(total/iterations))

np.testing.assert_array_almost_equal(out_grid[2:-2, 2:-2], out_image2[2:-2, 2:-2])

kernel = Kernel(backend='ocl')
kernel.kernel(in_grid, out_grid)
total = 0.0
for _ in range(iterations):
    with Timer() as t:
        kernel.kernel(in_grid, out_grid)
    total += t.interval
print("Specialized time avg without compile {0}".format(total/iterations))

# print("Print numpy out_image: ", out_image)
# print("Print numpy out_image2: ", out_image2)
# print("StencilGrid out_grid:", out_grid)
