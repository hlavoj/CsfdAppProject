import os
import hashlib
import xml.etree.ElementTree as ET
from typing import Optional
import httpx
from passlib.hash import md5_crypt

WEBSHARE_BASE = "https://webshare.cz/api"

# Module-level token cache
_wst: Optional[str] = None


def _hash_password(password: str, salt: str) -> str:
    """SHA1( unix-md5crypt(password, salt) ) — as required by Webshare."""
    mc = md5_crypt.using(salt=salt).hash(password)
    return hashlib.sha1(mc.encode()).hexdigest()


def _parse_xml_text(xml_text: str, tag: str) -> Optional[str]:
    try:
        root = ET.fromstring(xml_text)
        el = root.find(tag)
        return el.text if el is not None else None
    except ET.ParseError:
        return None


def _parse_xml_status(xml_text: str) -> str:
    return _parse_xml_text(xml_text, "status") or "UNKNOWN"


async def _get_salt(client: httpx.AsyncClient, username: str) -> str:
    resp = await client.post(
        f"{WEBSHARE_BASE}/salt/",
        data={"username_or_email": username},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    salt = _parse_xml_text(resp.text, "salt")
    if not salt:
        raise RuntimeError(f"Failed to get salt. Response: {resp.text}")
    return salt


async def _login(client: httpx.AsyncClient, username: str, password: str, salt: str) -> str:
    password_hash = _hash_password(password, salt)
    resp = await client.post(
        f"{WEBSHARE_BASE}/login/",
        data={
            "username_or_email": username,
            "password": password_hash,
            "digest": "0",
            "keep_logged_in": "1",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp.raise_for_status()
    status = _parse_xml_status(resp.text)
    if status != "OK":
        raise RuntimeError(f"Webshare login failed with status: {status}. Response: {resp.text}")
    token = _parse_xml_text(resp.text, "token")
    if not token:
        raise RuntimeError(f"No token in login response: {resp.text}")
    return token


async def get_token() -> str:
    global _wst
    if _wst:
        return _wst
    username = os.getenv("WEBSHARE_USERNAME")
    password = os.getenv("WEBSHARE_PASSWORD")
    async with httpx.AsyncClient() as client:
        salt = await _get_salt(client, username)
        _wst = await _login(client, username, password, salt)
    return _wst


async def _ensure_authenticated() -> str:
    return await get_token()


def _parse_search_results(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise RuntimeError(f"Failed to parse search XML: {e}\nResponse: {xml_text[:500]}")

    status = root.findtext("status")
    if status and status != "OK":
        raise RuntimeError(f"Webshare search failed: {status}")

    results = []
    for file_el in root.findall(".//file"):
        ident = file_el.findtext("ident")
        name = file_el.findtext("name")
        size_text = file_el.findtext("size") or "0"
        try:
            size = int(size_text)
        except ValueError:
            size = 0
        if ident and name:
            results.append({"ident": ident, "name": name, "size": size})
    return results


async def search_videos(query: str, limit: int = 20) -> list[dict]:
    global _wst
    wst = await _ensure_authenticated()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{WEBSHARE_BASE}/search/",
            data={
                "what": query,
                "category": "video",
                "limit": str(limit),
                "wst": wst,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()

    # Handle auth failure
    status = _parse_xml_status(resp.text)
    if status == "FATAL":
        _wst = None
        wst = await _ensure_authenticated()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{WEBSHARE_BASE}/search/",
                data={
                    "what": query,
                    "category": "video",
                    "limit": str(limit),
                    "wst": wst,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()

    return _parse_search_results(resp.text)


async def get_file_link(ident: str) -> str:
    global _wst
    wst = await _ensure_authenticated()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{WEBSHARE_BASE}/file_link/",
            data={"ident": ident, "wst": wst},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()

    link = _parse_xml_text(resp.text, "link")
    if not link:
        raise RuntimeError(f"No link in file_link response for ident={ident}: {resp.text}")
    return link
