"""Save the login QR inside the private browser profile on a headless server."""

from pathlib import Path

path = Path("/opt/MediaCrawler/tools/crawler_util.py")
source = path.read_text()
old = '    del ImageShow.UnixViewer.options["save_all"]\n    new_image.show()'
replacement = """    from pathlib import Path
    output = Path(".chrome_debug_profile/login_qrcode.png")
    output.parent.mkdir(parents=True, exist_ok=True)
    new_image.save(output)"""
if old in source:
    path.write_text(source.replace(old, replacement))
elif 'output = Path(".chrome_debug_profile/login_qrcode.png")' not in source:
    raise RuntimeError("Unknown MediaCrawler QR implementation; inspect before patching")

# Give the admin QR workflow ten minutes; old vendor revisions waited two minutes.
login_path = Path('/opt/MediaCrawler/media_platform/xhs/login.py')
login_source = login_path.read_text()
login_source = login_source.replace('stop_after_attempt(120)', 'stop_after_attempt(600)')
login_source = login_source.replace('remaining time is 120s', 'remaining time is 600s')
login_path.write_text(login_source)
