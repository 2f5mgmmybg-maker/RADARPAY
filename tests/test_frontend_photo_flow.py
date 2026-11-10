import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
PHOTOS = json.loads((ROOT / "photos.json").read_text(encoding="utf-8"))


class FrontendPhotoFlowTests(unittest.TestCase):
    def test_gallery_cards_keep_stable_photo_ids(self):
        self.assertIn('d.dataset.photoId=String(p.id)', HTML)
        self.assertIn('PHOTOS.find(function(item){return String(item.id)===card.dataset.photoId;})', HTML)

    def test_click_delegation_survives_gallery_rebuild(self):
        self.assertIn('grid.addEventListener("click",function(e){activatePhoto(e.target)},true)', HTML)
        self.assertIn('grid.innerHTML=""', HTML)

    def test_lightbox_can_close_and_reopen_without_stale_image_state(self):
        self.assertIn('function closeLb()', HTML)
        self.assertIn('CUR=null', HTML)
        self.assertIn('function openLb(p)', HTML)
        self.assertIn('CUR=p', HTML)
        self.assertIn('im.removeAttribute("src")', HTML)

    def test_image_loading_has_retry_and_visible_failure_recovery(self):
        self.assertIn('function loadPhotoImage(img,src,onFailure)', HTML)
        self.assertIn('if(retries<3)', HTML)
        self.assertIn('radar_retry=', HTML)
        self.assertIn('https://raw.githubusercontent.com/2f5mgmmybg-maker/RADARPAY/main/', HTML)
        self.assertIn('photo-unavailable', HTML)
        self.assertIn('Photo indisponible pour le moment', HTML)

    def test_async_lightbox_actions_use_the_captured_target_id(self):
        self.assertIn('var targetId=current.id', HTML)
        self.assertIn('call("/api/update",{id:targetId,prix:nextPrice})', HTML)
        self.assertIn('call("/api/want",{id:targetId,client_offer:offer})', HTML)

    def test_every_catalogue_photo_has_a_real_local_asset(self):
        for photo in PHOTOS.get("photos", []):
            with self.subTest(photo=photo.get("id")):
                self.assertTrue(photo.get("id"))
                self.assertTrue(photo.get("image"))
                self.assertTrue((ROOT / "photos" / photo["image"]).is_file())


if __name__ == "__main__":
    unittest.main()
