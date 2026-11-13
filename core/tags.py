# -*- coding: utf-8 -*-
"""元数据（ID3 / Vorbis / MP4 / FLAC）读写，基于 mutagen。"""
from __future__ import annotations

import os
from typing import Dict, Optional

try:
    import mutagen
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, TRCK, COMM, APIC, TPE2
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggvorbis import OggVorbis
    from mutagen.asf import ASF
    HAVE = True
except Exception:  # noqa: BLE001
    HAVE = False

FIELD_MAP = {
    "title": ["title", "TIT2", "\xa9nam", "Title"],
    "artist": ["artist", "TPE1", "\xa9ART", "Author"],
    "album": ["album", "TALB", "\xa9alb", "Album"],
    "album_artist": ["albumartist", "TPE2", "aART", "AlbumArtist"],
    "date": ["date", "TDRC", "\xa9day", "Year"],
    "genre": ["genre", "TCON", "\xa9gen", "Genre"],
    "comment": ["comment", "COMM", "\xa9cmt", "Description"],
    "track": ["tracknumber", "TRCK", "trkn", "TrackNumber"],
}


def read_tags(path: str) -> Dict[str, str]:
    if not HAVE or not os.path.exists(path):
        return {}
    out: Dict[str, str] = {}
    try:
        audio = mutagen.File(path, easy=True)
        if audio and audio.tags:
            for key in ("title", "artist", "album", "albumartist", "date", "genre", "tracknumber", "comment"):
                val = audio.tags.get(key)
                if val:
                    out[key if key != "albumartist" else "album_artist"] = str(val[0])
    except Exception:
        pass
    if not out:
        try:
            audio = mutagen.File(path)
            if audio and audio.tags:
                for k, v in audio.tags.items():
                    if isinstance(v, list) and v and isinstance(v[0], str):
                        out.setdefault(k.lower(), v[0])
        except Exception:
            pass
    return out


def write_tags(path: str, meta: Dict[str, str]) -> bool:
    if not HAVE or not meta:
        return False
    meta = {k: str(v) for k, v in meta.items() if v not in (None, "")}
    if not meta:
        return False
    try:
        audio = mutagen.File(path, easy=True)
        if audio is None:
            return False
        if audio.tags is None:
            try:
                audio.add_tags()
            except Exception:
                pass
        mapping = {
            "title": "title", "artist": "artist", "album": "album",
            "album_artist": "albumartist", "date": "date", "genre": "genre",
            "comment": "comment",
        }
        for key, tag in mapping.items():
            if key in meta:
                try:
                    audio[tag] = [meta[key]]
                except Exception:
                    pass
        audio.save()
        return True
    except Exception:
        return False


def embed_cover(path: str, image_path: str) -> bool:
    if not HAVE or not os.path.exists(image_path):
        return False
    try:
        with open(image_path, "rb") as fh:
            blob = fh.read()
        ext = image_path.rsplit(".", 1)[-1].lower()
        audio = mutagen.File(path)
        if audio is None:
            return False
        if isinstance(audio, FLAC):
            pic = Picture()
            pic.data = blob
            pic.type = 3
            pic.mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
            audio.clear_pictures()
            audio.add_picture(pic)
        elif isinstance(audio, MP4):
            fmt = MP4Cover.FORMAT_JPEG if ext in ("jpg", "jpeg") else MP4Cover.FORMAT_PNG
            audio["covr"] = [MP4Cover(blob, imageformat=fmt)]
        elif hasattr(audio, "tags") and not isinstance(audio, OggVorbis):
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=blob))
        audio.save()
        return True
    except Exception:
        return False
