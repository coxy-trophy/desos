"""The DesOS bird: one drawing, shared with DEs-UI, read without an SVG renderer."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'branding'))
import bird  # noqa: E402

DES_UI = ROOT.parent / 'des-ui' / 'branding' / 'bird.svg'


class BirdTests(unittest.TestCase):
    def test_the_drawing_is_five_pieces(self):
        # The upper wing, the body and head, the lower wing, the eye and the tail.
        self.assertEqual(len(bird.polygons()), 5)

    def test_the_mask_is_drawn_by_its_ink_with_the_eye_open(self):
        mask = bird.mask(100)
        self.assertEqual(mask.size, (86, 100))
        self.assertEqual(mask.getpixel((65, 80)), 0, 'empty where the tail curves round')
        self.assertGreater(mask.histogram()[255], 400)

    @unittest.skipUnless(DES_UI.is_file(), 'DEs-UI checkout not beside this one')
    def test_the_copy_matches_des_ui_master(self):
        self.assertEqual(bird.SVG.read_bytes(), DES_UI.read_bytes(),
                         'copy des-ui/branding/bird.svg to branding/bird.svg and regenerate the artwork')


if __name__ == '__main__':
    unittest.main()
