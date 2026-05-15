import io
import json
import re
from urllib.parse import quote

import fastapi
from fastapi import Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from mutagen.id3 import TPE1, TIT2, TALB, APIC, USLT, SYLT
from mutagen.mp3 import MP3
from yandex_music import *
from yandex_music.exceptions import UnauthorizedError, NotFoundError

config = json.load(open('config.json'))

app: fastapi.FastAPI = fastapi.FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "supported_standard_versions": ["MBs-1.0"],  # TODO: Make autodetect
        "provider_identifier": f"{config['information']['provider_id']}",
        "provider_repository": f"{config['information']['provider_repository']}",
        "logo": config["information"]["provider_logo"],
        "color": config["information"]["provider_color"]
    }


@app.get("/v1.0")
async def api_root(request: Request):
    return {
        "premium": check_premium_by_token(request.headers.get("Authorization"))
    }


def client_by_token(token: str) -> Client:
    if not token:
        raise fastapi.HTTPException(status_code=401, detail="Bad Token")
    try:
        client = Client(token).init()
    except UnauthorizedError:
        raise fastapi.HTTPException(status_code=401, detail="Bad Token")

    return client


def check_premium_by_token(key: str):
    return check_premium_by_client(client_by_token(key))


def check_premium_by_client(client: Client):
    result = client.accountStatus().plus.has_plus
    if not result:
        raise HTTPException(status_code=403, detail="Premium subscription required")
    return result


@app.get("/v1.0/auth", response_class=HTMLResponse)
async def auth():
    with open("auth.html", "r") as f:
        return f.read()


@app.get("/v1.0/search/{search_text}")
def search(request: Request, search_text: str):
    results = []

    client = client_by_token(request.headers.get("Authorization"))

    try:
        if search_text.__contains__("trackId:"):
            results.append(get_result_by_id(int(search_text.split(":")[1]), client))
        elif search_text.__contains__("albumId:"):
            results = get_tracks_by_album_id(int(search_text.split(":")[1]), client)
        elif search_text.__contains__("artistId:"):
            results = get_tracks_by_artist_id(int(search_text.split(":")[1]), client)
        elif not results:
            for searchable_track in client.search(search_text).tracks.results:
                results.append(get_result_by_track(searchable_track))
    except Exception as e:
        print(str(e))
    return results


@app.get("/v1.0/download/{track_id}")
async def download(request: Request, track_id: str):
    client = client_by_token(request.headers.get("Authorization"))
    track = get_track_by_id(int(track_id), client)

    title = track.title
    artist_name = track.artists[0].name
    album_name = track.albums[0].title
    cover_bytes = track.albums[0].download_cover_bytes()

    lyrics_text: str = ""
    lyrics_lrc: str = ""
    lyricist: str = "No lyrics provider"

    try:
        lyrics_text = get_lyrics(track, "text", client)["lyrics"]
    except Exception:
        pass

    try:
        lyrics_lrc = get_lyrics(track, "lrc", client)["lyrics"]
    except Exception:
        pass

    try:
        lyricist = track.getLyrics().major.name
    except Exception:
        pass

    data = track.download_bytes()
    buffer = io.BytesIO(data)

    try:
        audio = MP3(buffer)
        if audio.tags is None:
            audio.add_tags()
    except Exception:
        buffer.seek(0)
        audio = MP3(buffer)
        audio.add_tags()

    audio.tags.add(TPE1(encoding=3, text=artist_name))
    audio.tags.add(TIT2(encoding=3, text=title))
    audio.tags.add(TALB(encoding=3, text=album_name))
    audio.tags.add(APIC(
        encoding=3,
        mime="image/jpeg",
        type=3,
        desc="cover",
        data=cover_bytes
    ))

    if lyrics_lrc or lyrics_text:
        audio.tags.add(USLT(
            encoding=3,
            lang='XXX',
            desc=lyricist,
            text=lyrics_lrc if lyrics_lrc else lyrics_text
        ))

    if lyrics_lrc:
        audio.tags.add(SYLT(
            encoding=3,
            lang='XXX',
            format=2,
            type=1,
            text=parse_lrc_to_sylt(lyrics_lrc)
        ))

    audio.save(buffer)

    buffer.seek(0)

    filename = f"{artist_name} - {title}.mp3"
    encoded_filename = quote(filename)

    return Response(
        content=buffer.read(),
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"
        }
    )


@app.get("/v1.0/lyrics/{lyrics_type}/{track_id}")
def lyrics(request: Request, lyrics_type: str, track_id: str):
    client = client_by_token(request.headers.get("Authorization"))

    check_premium_by_client(client)

    return get_lyrics(get_track_by_id(int(track_id), client), lyrics_type, client)


def parse_lrc_to_sylt(lrc_text):
    sylt_data = []
    pattern = re.compile(r'\[(\d+):(\d+\.\d+)]\s*(.*)')

    for line in lrc_text.splitlines():
        match = pattern.match(line)
        if match:
            minutes = int(match.group(1))
            seconds = float(match.group(2))
            text = match.group(3).strip()

            milliseconds = int((minutes * 60 + seconds) * 1000)
            sylt_data.append((text, milliseconds))

    return sylt_data


def get_lyrics(searchable_track: Track, lyrics_type: str, client: Client):
    match lyrics_type:
        case "text":
            if not searchable_track.lyrics_info.has_available_text_lyrics:
                raise HTTPException(status_code=405, detail="Lyrics of this type are not available")
            else:
                return {
                    "type": "text",
                    "lyrics": searchable_track.get_lyrics("TEXT").fetchLyrics()
                }
        case "lrc":
            if not searchable_track.lyrics_info.has_available_sync_lyrics:
                raise HTTPException(status_code=405, detail="Lyrics of this type are not available")
            else:
                return {
                    "type": "lrc",
                    "lyrics": client.tracks_lyrics(searchable_track.track_id, "LRC").fetchLyrics()
                }
    raise HTTPException(status_code=405, detail="This lyrics type are not available")


def get_tracks_by_album_id(album_id: int, client: Client):
    check_premium_by_client(client)

    searchable_album = client.albums_with_tracks(album_id)
    results = []
    for searchable_volume in searchable_album.volumes:
        for searchable_track in searchable_volume:
            results.append(get_result_by_track(searchable_track))
    return results


def get_tracks_by_artist_id(artist_id: int, client: Client):
    results = []

    for searchable_track in client.artists_tracks(artist_id).tracks:
        results.append(get_result_by_track(searchable_track))

    return results


def get_track_by_id(track_id: int, client: Client):
    if not track_id:
        raise HTTPException(status_code=404, detail="Track not found")
    try:
        return client.tracks(track_id)[0]
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Track not found")


def get_result_by_id(track_id: int, client: Client):
    return get_result_by_track(get_track_by_id(track_id, client))


def get_result_by_track(searchable_track: Track):
    return {
        "id": searchable_track.id,
        "title": searchable_track.title,
        "url": f"https://music.yandex.ru/track/{searchable_track.id}",
        "artist": {
            "id": searchable_track.artists[0].id,
            "name": searchable_track.artists[0].name,
            "url": f"https://music.yandex.ru/artist/{searchable_track.artists[0].id}",
        },
        "album": {
            "id": searchable_track.albums[0].id,
            "url": f"https://music.yandex.ru/album/{searchable_track.albums[0].id}",
            "name": searchable_track.albums[0].title,
            "logo": f"https://{searchable_track.albums[0].cover_uri}".replace("%%", "200x200")
        },
        "lyrics":
            {
                "text": searchable_track.lyrics_info.has_available_text_lyrics,
                "lrc": searchable_track.lyrics_info.has_available_sync_lyrics
            },
        "explicit": True if searchable_track.content_warning == "explicit" else False
    }


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config["port"])
