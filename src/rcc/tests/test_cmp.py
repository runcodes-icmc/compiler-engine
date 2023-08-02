from six import StringIO

import rcc.cmp
import unittest


test_a = """hello, world"""
test_b = """
	Hello, world
"""
test_c = """hello, world\r\t"""
test_d = """\tone two three four"""
test_e = """one two three four
five six seven

eight nine ten
"""
test_f = """1.0 1.2 1.3
2.0
3.0
"""
test_g = """1.0 1.3 1.2
1.5
2.5
"""


class TestCmp(unittest.TestCase):
    def test_text_cmp_identical(self):
        file1 = StringIO(test_a)
        file2 = StringIO(test_a)
        self.assertTrue(rcc.cmp.text_cmp(file1, file2))

    def test_text_cmp_empty_file(self):
        file1 = StringIO(test_a)
        file2 = StringIO()
        self.assertFalse(rcc.cmp.text_cmp(file1, file2))

    def test_text_cmp_equal(self):
        file1 = StringIO(test_a)
        file2 = StringIO(test_c)
        self.assertTrue(rcc.cmp.text_cmp(file1, file2))

    def test_text_cmp_different(self):
        file1 = StringIO(test_a)
        file2 = StringIO(test_b)
        self.assertFalse(rcc.cmp.text_cmp(file1, file2))

    def test_text_cmp2_identical(self):
        file1 = StringIO(test_a)
        file2 = StringIO(test_a)
        self.assertTrue(rcc.cmp.text_cmp2(file1, file2))

    def test_text_cmp2_empty_file(self):
        file1 = StringIO(test_a)
        file2 = StringIO()
        self.assertFalse(rcc.cmp.text_cmp2(file1, file2))

    def test_text_cmp2_equal(self):
        file1 = StringIO(test_a)
        file2 = StringIO(test_c)
        self.assertTrue(rcc.cmp.text_cmp2(file1, file2))

    def test_text_cmp2_almost_equal(self):
        file1 = StringIO(test_a)
        file2 = StringIO(test_b)
        self.assertTrue(rcc.cmp.text_cmp2(file1, file2))

    def test_text_cmp2_different(self):
        file1 = StringIO(test_d)
        file2 = StringIO(test_e)
        self.assertFalse(rcc.cmp.text_cmp2(file1, file2))

    def test_text_number_cmp_identical(self):
        file1 = StringIO(test_f)
        file2 = StringIO(test_f)
        self.assertTrue(rcc.cmp.number_cmp(file1, file2, 0.0))

    def test_text_number_cmp_empty_file(self):
        file1 = StringIO(test_f)
        file2 = StringIO()
        self.assertFalse(rcc.cmp.number_cmp(file1, file2, 0.0))

    def test_text_number_cmp_equal(self):
        file1 = StringIO(test_f)
        file2 = StringIO(test_g)
        self.assertTrue(rcc.cmp.number_cmp(file1, file2, 0.5))

    def test_text_number_cmp_different(self):
        file1 = StringIO(test_f)
        file2 = StringIO(test_g)
        self.assertFalse(rcc.cmp.number_cmp(file1, file2, 1e-3))
