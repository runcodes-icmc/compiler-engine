"""
A collection of file comparison functions for text-only or text and numeric
files, with varying strictness.
"""

from six.moves import zip, zip_longest


def text_cmp(file1, file2):
    if isinstance(file1, str):
        with open(file1, "r") as f1:
            return text_cmp(f1, file2)
    if isinstance(file2, str):
        with open(file2, "r") as f2:
            return text_cmp(file1, f2)
    try:
        for line1, line2 in zip_longest(file1, file2):
            if line1 == None or line2 == None:
                return False
            if line1.rstrip() != line2.rstrip():
                return False
        return True
    except UnicodeDecodeError:
        return False


def text_cmp2(file1, file2):
    if isinstance(file1, str):
        with open(file1, "r") as f1:
            return text_cmp2(f1, file2)
    if isinstance(file2, str):
        with open(file2, "r") as f2:
            return text_cmp2(file1, f2)
    try:
        while True:
            line1 = next(file1, None)
            line2 = next(file2, None)
            if line1 == None or line2 == None:
                break
            while line1.strip() == "":
                line1 = next(file1, None)
                if line1 == None:
                    return False
            while line2.strip() == "":
                line2 = next(file2, None)
                if line2 == None:
                    return False
            if line1 != line2:
                tokens1 = line1.lower().split()
                tokens2 = line2.lower().split()
                for tok1, tok2 in zip_longest(tokens1, tokens2):
                    if tok1 != tok2:
                        return False
        return line1 == line2
    except UnicodeDecodeError:
        return False


def number_cmp(file1, file2, abs_error):
    if isinstance(file1, str):
        with open(file1, "r") as f1:
            return number_cmp(f1, file2, abs_error)
    if isinstance(file2, str):
        with open(file2, "r") as f2:
            return number_cmp(file1, f2, abs_error)

    def is_float(s):
        try:
            float(s)
            return True
        except ValueError:
            return False

    if abs_error < 0.0:
        raise ValueError("Error must be positive")
    try:
        for line1, line2 in zip_longest(file1, file2):
            if line1 == None or line2 == None:
                return False
            tokens1 = line1.split()
            tokens2 = line2.split()
            if len(tokens1) != len(tokens2):
                return False
            for tok1, tok2 in zip(tokens1, tokens2):
                if is_float(tok1) and is_float(tok2):
                    if abs(float(tok1) - float(tok2)) > abs_error:
                        return False
                elif tok1 != tok2:
                    return False
        return True
    except UnicodeDecodeError:
        return False
