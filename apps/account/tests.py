import json
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from account.models import User
from account.services.auth_service import issue_access_token


class AccountProfileAPITests(TestCase):
    profile_url = "/pldp/api/account/profile/"

    def setUp(self):
        self.user = User.objects.create_user(
            username="wx_profile_test",
            wx_openid="openid-profile-test",
            wx_nickname="原昵称",
            phone="13800000000",
        )
        self.token = issue_access_token(self.user)
        self.authorization = f"Bearer {self.token}"
        self.media_directory = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(
            MEDIA_ROOT=self.media_directory.name,
            MEDIA_URL="/media/",
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.media_directory.cleanup)

    def test_profile_requires_access_token(self):
        response = self.client.get(self.profile_url)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], 401)

    def test_profile_returns_current_account_identity(self):
        response = self.client.get(
            self.profile_url,
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["openid"], self.user.wx_openid)
        self.assertEqual(data["nickname"], "原昵称")
        self.assertEqual(data["phone"], "13800000000")
        self.assertEqual(data["role"], "tourist")

    def test_profile_updates_only_editable_account_fields(self):
        response = self.client.post(
            self.profile_url,
            data=json.dumps(
                {
                    "nickname": "新昵称",
                    "phone": "13900000000",
                    "role": "factory_admin",
                    "company_name": "不可由个人资料修改",
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.wx_nickname, "新昵称")
        self.assertEqual(self.user.phone, "13900000000")
        self.assertEqual(response.json()["data"]["role"], "tourist")
        self.assertEqual(response.json()["data"]["company_name"], "")

    def test_profile_accepts_persistent_avatar_upload(self):
        avatar = SimpleUploadedFile(
            "avatar.png",
            b"\x89PNG\r\n\x1a\n" + (b"\x00" * 24),
            content_type="image/png",
        )

        response = self.client.post(
            self.profile_url,
            data={"nickname": "头像用户", "phone": "13800000000", "avatar": avatar},
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertIn("/media/account/avatars/", self.user.wx_avatar)
        self.assertEqual(response.json()["data"]["avatar_url"], self.user.wx_avatar)

    def test_profile_rejects_invalid_phone(self):
        response = self.client.post(
            self.profile_url,
            data={"nickname": "测试用户", "phone": "138abc"},
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone, "13800000000")

    def test_profile_rejects_fake_image_upload(self):
        avatar = SimpleUploadedFile(
            "fake.png",
            b"this-is-not-an-image",
            content_type="image/png",
        )

        response = self.client.post(
            self.profile_url,
            data={"nickname": "测试用户", "phone": "", "avatar": avatar},
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1)
        self.user.refresh_from_db()
        self.assertFalse(self.user.wx_avatar)
