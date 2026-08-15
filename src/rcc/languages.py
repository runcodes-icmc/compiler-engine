from collections.abc import Iterable
from typing import override

IMAGE_FORMAT = "ghcr.io/runcodes-icmc/compiler-images-%s:latest"


class Language:
    name: str
    extensions: list[str]
    compilable: bool
    image: str

    def __init__(
        self,
        name: str | None,
        extensions: Iterable[str],
        compilable: bool = True,
        image_name: str | None = None,
    ) -> None:
        if name is None:
            raise ValueError("Language name must not be None")

        if not hasattr(extensions, "__iter__"):
            raise ValueError("Language extensions must be iterable")

        self.name = str(name)
        self.extensions = list(extensions)
        self.compilable = compilable
        self.image = IMAGE_FORMAT % (image_name or self.name.lower())

    @property
    def standard_extension(self) -> str | None:
        if len(self.extensions) == 0:
            return None

        return self.extensions[0]

    @override
    def __str__(self) -> str:
        return f"Language({self.name}): [{', '.join(self.extensions)}] - {self.image}"

    @override
    def __repr__(self) -> str:
        return str(self)


###################
# Known Languages #
###################

KNOWN_LANGUAGES: list[Language] = [
    Language("C", ["c", "h"]),
    Language("C++", ["cpp", "cc", "cxx", "c++", "hpp", "h"], image_name="cpp"),
    Language("C#", ["cs"], image_name="dotnet"),
    Language("Fortran", ["f", "f90", "f95", "f15", "f03"]),
    Language("Golang", ["go"], image_name="go"),
    Language("Haskell", ["hs", "lhs"]),
    Language("Java", ["java", "jar", "class"]),
    Language("Octave", ["m"], False),
    Language("Pascal", ["pas", "pp", "pascal"]),
    Language("Portugol", ["por"]),
    Language("Python", ["py", "py3", "pyc"], False),
    Language("R", ["r"], False),
    Language("Rust", ["rs"]),
    Language("Lua", ["lua", "lol", "lu", "luac"]),
    Language("Julia", ["jl"], False),
    Language("Prolog", ["pl", "pro", "prolog"]),
    Language("C (OpenMP)", ["omp.c", "omp.h"], image_name="c-omp"),
    Language(
        "C++ (OpenMP)",
        ["omp.cpp", "omp.cc", "omp.cxx", "omp.c++", "omp.hpp", "omp.h"],
        image_name="cpp-omp",
    ),
    Language("C (OpenMP + MPI)", ["mpi.c", "mpi.h"], image_name="c-omp-mpi"),
    Language(
        "C++ (OpenMP + MPI)",
        ["mpi.cpp", "mpi.cc", "mpi.cxx", "mpi.c++", "mpi.hpp", "mpi.h"],
        image_name="cpp-omp-mpi",
    ),
    Language("Verilog", ["v", "vh"], False),
    Language("Zig", ["zig"]),
]

_LANGUAGE_EXTENSIONS_MAPPING: dict[str, Language | None] = {}
for lang in KNOWN_LANGUAGES:
    for ext in lang.extensions:
        # Avoid ambiguous extensions
        if ext in _LANGUAGE_EXTENSIONS_MAPPING:
            _LANGUAGE_EXTENSIONS_MAPPING[ext] = None
        else:
            _LANGUAGE_EXTENSIONS_MAPPING[ext] = lang


def language_from_extension(ext_or_filename: str) -> Language | None:
    """Retrieve the language associated with the given extension.

    Args:
        ext_or_filename (str): target extension or filename

    Returns:
        Optional[Language]: the language associated with the extension, when there is a single match, None otherwise
    """
    exts = ext_or_filename.split(".")
    ext = exts[-1].lower()

    # Handle OpenMP and MPI extensions
    if ext in ("c", "h", "cpp", "cc", "cxx", "c++", "hpp") and len(exts) == 3:
        pre_ext = exts[-2].lower()
        if pre_ext in ("omp", "mpi"):
            ext = f"{pre_ext}.{ext}"

    return _LANGUAGE_EXTENSIONS_MAPPING.get(ext, None)
