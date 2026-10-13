import pytest

from app.services.cos_service import CosUploadError, upload_author_avatar


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://sns-avatar-qc.xhscdn.com/avatar.jpg",
        "https://xhscdn.com.attacker.test/avatar.jpg",
        "https://127.0.0.1/avatar.jpg",
        "file:///etc/passwd",
    ],
)
async def test_avatar_upload_rejects_unsafe_hosts_without_network(url):
    with pytest.raises(CosUploadError):
        await upload_author_avatar(url)
