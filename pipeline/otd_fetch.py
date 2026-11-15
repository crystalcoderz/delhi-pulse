"""Fetch Delhi static GTFS from OTD via its public purpose-form download flow."""
import re
import sys
import zipfile
from pathlib import Path

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "data/otd_gtfs.zip")


def try_value(label):
    url = "https://otd.delhi.gov.in/data/static/"
    c = httpx.Client(headers={"User-Agent": UA, "Referer": url,
                               "Accept-Language": "en-US,en;q=0.9"},
                     follow_redirects=True, timeout=httpx.Timeout(300.0, connect=30.0))
    r = c.get(url)
    print(f"GET {r.status_code}, {len(r.content)} bytes, cookies: {list(c.cookies)}")
    m = re.search(r"name=['\"]csrfmiddlewaretoken['\"] value=['\"]([^'\"]+)", r.text)
    if not m:
        print("no csrf token found; page head:", r.text[:300])
        return False
    tok = m.group(1)
    data = {
        "csrfmiddlewaretoken": tok,
        "dataDownloaded": label,
        "usageType": "NonCommercial",
        "purpose": "R&D",
        "name": "Delhi Pulse",
        "email": "praya.delhipulse@gmail.com",
    }
    r2 = c.post(url, data=data)
    ct = r2.headers.get("content-type", "")
    print(f"POST({label}) -> {r2.status_code}, content-type={ct}, {len(r2.content)} bytes")
    if r2.status_code == 200 and ("zip" in ct or r2.content[:2] == b"PK"):
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_bytes(r2.content)
        print(f"saved -> {OUT}")
        if zipfile.is_zipfile(OUT):
            with zipfile.ZipFile(OUT) as z:
                for info in z.infolist():
                    print(f"  {info.filename}  {info.file_size/1e6:.1f} MB")
                z.extractall(OUT.parent / "otd_gtfs")
                print(f"extracted -> {OUT.parent / 'otd_gtfs'}")
            return True
        print("not a zip; head:", r2.content[:120])
    else:
        hits = re.findall(r"(error|Error)[^<]{0,80}", r2.text[:2000])
        print("page errors:", hits[:3])
    return False


if __name__ == "__main__":
    for label in ("stops", "routes", "trips", "stop_times", "shapes", "all"):
        if try_value(label):
            sys.exit(0)
    sys.exit(1)
