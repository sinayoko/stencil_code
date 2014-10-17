from numpy import *
import sys
from stencil_code.stencil_grid import StencilGrid
from stencil_code.stencil_kernel import StencilKernel

print("This example hasn't been update to the current API")
exit(1)

class SpecializedLaplacian27(StencilKernel):

    def distance(self, x, y):
        """
        override the StencilKernel distance
        use manhattan distance
        """
        return sum([abs(x[i]-y[i]) for i in range(len(x))])

    def kernel(self, source_data, factors, output_data):
        for x in source_data.interior_points():
            for n in source_data.neighbors(x, 2):
                output_data[x] += factors[self.distance(x, n)] * source_data[n]

    def inner_summation_kernel(self, neighbor_value, coefficient):
        return neighbor_value * coefficient

def laplacian_27pt(nx, ny, nz, alpha, beta, gamma, delta, IN, OUT):
    """
    Original version of laplacian
    """
    for k in range(2, nz - 1):
        for j in range(2, ny - 1):
            for i in range(2, nx - 1):
                OUT[i, j, k] = alpha * IN[i, j, k] + \
                    beta * (IN[i + 1, j, k] + IN[i - 1, j, k] +
                            IN[i, j + 1, k] + IN[i, j - 1, k] +
                            IN[i, j, k + 1] + IN[i, j, k - 1]) + \
                    gamma * (IN[i - 1, j, k - 1] + IN[i - 1, j - 1, k] +
                             IN[i - 1, j + 1, k] + IN[i - 1, j, k + 1] +
                             IN[i, j - 1, k - 1] + IN[i, j + 1, k - 1] +
                             IN[i, j - 1, k + 1] + IN[i, j + 1, k + 1] +
                             IN[i + 1, j, k - 1] + IN[i + 1, j - 1, k]) + \
                    delta * (IN[i - 1, j - 1, k - 1] + IN[i - 1, j + 1, k - 1] +
                             IN[i - 1, j - 1, k + 1] + IN[i - 1, j + 1, k + 1] +
                             IN[i + 1, j - 1, k - 1] + IN[i + 1, j + 1, k - 1] +
                             IN[i + 1, j - 1, k + 1] + IN[i + 1, j + 1, k + 1])


def build_data(nx, ny, nz):

    input = StencilGrid([nx, ny, nz])
    input.set_neighborhood(2, input.moore_neighborhood(include_origin=True))
    # input.set_neighborhood(2, [
    #     (0, 0, 0),
    #     (1, 0, 0), (0, 1, 0), (0, 0, 1),
    #     (-1, -1, 0), (-1, 1, 0), (-1, 0, -1), (-1, 0, 1),
    #     (0, -1, -1), (0, -1, 1), (0, 1, -1), (0, 1, 1),
    #     (-1, -1, -1), (-1, -1, 1), (-1, 1, -1), (-1, 1, 1),
    #     (1, -1, -1), (1, -1, 1), (1, 1, -1), (1, 1, 1),
    # ])
    for x in input.interior_points():
        input[x] = random.random()
    coefficients = StencilGrid([4])
    coefficients[0] = 1.0
    coefficients[1] = 0.5
    coefficients[2] = 0.25
    coefficients[3] = 0.125

    for n in input.neighbor_definition:
        print(n)

    output = StencilGrid([nx,ny,nz])

    return input, coefficients, output

if __name__ == '__main__':
    nx = int(sys.argv[1])
    ny = int(sys.argv[2])
    nz = int(sys.argv[3])

    input_grid, coefficients, output = build_data(nx, ny, nz)
    SpecializedLaplacian27().kernel(input_grid, coefficients, output)

    print(output)
    exit(1)

    IN = (random.rand(nx*ny*nz)).reshape(nx,ny,nz)
    OUT = (random.rand(nx*ny*nz)).reshape(nx,ny,nz)
    alpha = 1.0
    beta = 0.5
    gamma = 0.25
    delta = 0.125
    laplacian_27pt(nx, ny, nz, alpha, beta, gamma, delta, IN, OUT)
