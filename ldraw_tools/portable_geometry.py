"""Portable source-point expansion for the pinned pyldraw3 1.7 library.

The reviewed Technic registry hashes exact expanded coordinates. BLAS may fuse
multiply/add operations differently across CPUs, changing the final bits of
those coordinates without changing a source file. Evaluate source-reference
matrix/point products in an explicit scalar order instead. This reproduces the
existing reviewed hashes; it does not round coordinates or approve new geometry.

Only matrices parsed by this Parts instance are adapted. The dependency and its
global Matrix class remain untouched, including model-level placement math.
Revalidate this adapter and the registry when upgrading pyldraw3.
"""
from ldraw import Matrix, Parts, Piece, Vector
from ldraw.part import Part


class SourcePointMatrix(Matrix):
    def __mul__(self, other):
        if isinstance(other, Vector):
            return Vector(*(row[0] * other.x + row[1] * other.y + row[2] * other.z
                            for row in self.rows))
        return super().__mul__(other)


class SourcePointPart(Part):
    @property
    def objects(self):
        for obj in super().objects:
            if isinstance(obj, Piece):
                obj.matrix = SourcePointMatrix(obj.matrix.rows)
            yield obj


class PortableParts(Parts):
    def part(self, description=None, code=None):
        part = super().part(description=description, code=code)
        return SourcePointPart(part.path)
