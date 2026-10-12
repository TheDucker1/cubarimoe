from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from downloads.identity import identity
from downloads.models import DownloadedChapter, SeriesSnapshot
from .test_workflows import dataset


class LibraryTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("library-owner", is_staff=True))
        self.records = {}
        for number, group, status in (("1", "Team", "ready"), ("2", "Team", "missing"),
                                      ("3", "Other", "ready"), ("10", "Team", "ready"),
                                      ("4", "Team", "deleting")):
            record = DownloadedChapter.objects.create(
                chapter_key=identity("gist", "series", number, group), source="gist", series="series",
                chapter=number, group=group, title="Example", status=status,
                manifest=[{"name": "0001.png", "type": "image/png"}],
            )
            self.records[number] = record
        SeriesSnapshot.objects.create(source="gist", series="series", data=dataset())
        self.anchor = self.records["1"].chapter_key
        self.api = "/library/api/series/{}/".format(self.anchor)

    def test_library_groups_ready_chapters_and_searches_without_remote_requests(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No upstream or S3 requests")):
            response = self.client.get("/library/")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Example")
            self.assertEqual([r.chapter for r in response.context["titles"][0]["chapters"]], ["1", "3", "10"])
            self.assertContains(response, reverse("library-reader", args=(self.anchor, "1", 1)))
            self.assertEqual(len(self.client.get("/library/?q=EXAMPLE").context["titles"]), 1)
            self.assertContains(self.client.get("/library/?q=absent"), "No downloaded titles match")
        self.assertEqual(DownloadedChapter.objects.count(), 5)

    def test_library_requires_staff_but_reading_keeps_existing_public_boundary(self):
        self.client.logout()
        self.assertEqual(self.client.get("/library/").status_code, 401)
        self.assertEqual(self.client.get(self.api).status_code, 200)
        user = get_user_model().objects.create_user("ordinary")
        self.client.force_login(user)
        self.assertEqual(self.client.get("/library/").status_code, 403)

    def test_stored_api_excludes_missing_deleting_and_other_group_even_with_upstream_flag(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No remote resolution")):
            data = self.client.get(self.api + "?upstream=1").json()
        self.assertEqual(list(data["chapters"]), ["1", "10"])
        self.assertEqual(data["groups"], {"0": "Team"})
        for chapter in data["chapters"].values():
            self.assertTrue(all(page["src"].startswith("/stored-chapters/") for page in chapter["groups"]["0"]))
        self.assertNotIn("cover", data["chapters"]["1"])
        self.assertEqual(data["cover"], "")

    def test_stored_reader_renders_existing_reader_without_upstream_fallback(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No remote resolution")):
            response = self.client.get(reverse("library-reader", args=(self.anchor, "10", 1)))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="rdr-main"')
        self.assertContains(response, 'const BASE_API_PATH = "/library/api/series/"')
        self.assertNotContains(response, 'class="d-source-fallback"')
        self.assertEqual(self.client.get(reverse("library-reader", args=(self.anchor, "2", 1))).status_code, 404)
        self.assertEqual(self.client.get(reverse("library-reader", args=(self.anchor, "3", 1))).status_code, 404)
        self.assertEqual(self.client.get(reverse("library-reader", args=(self.anchor, "1", 0))).status_code, 404)

    def test_deleted_anchor_does_not_hide_remaining_stored_chapters(self):
        self.records["1"].status = "missing"
        self.records["1"].save()
        self.assertEqual(list(self.client.get(self.api).json()["chapters"]), ["10"])
        self.records["10"].status = "deleting"
        self.records["10"].save()
        self.assertEqual(self.client.get(self.api).status_code, 404)

    def test_stale_or_absent_snapshot_never_removes_stored_chapters_or_resolves_upstream(self):
        SeriesSnapshot.objects.all().delete()
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No remote resolution")):
            data = self.client.get(self.api).json()
        self.assertEqual(list(data["chapters"]), ["1", "10"])
        self.assertEqual(data["chapters"]["10"]["volume"], "1")

    def test_library_returns_empty_state_and_reader_title_link_returns_to_library(self):
        DownloadedChapter.objects.update(status="missing")
        self.assertContains(self.client.get("/library/"), "No chapters downloaded yet")
        self.assertRedirects(self.client.get("/library/read/{}/".format(self.anchor)), "/library/")
