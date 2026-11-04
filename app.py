#!/usr/bin/env python3
"""InMusic local music library server. Python standard library only."""
from __future__ import annotations
import json, mimetypes, os, re, sys, threading, time, uuid, hashlib, secrets, hmac
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

BASE = Path(__file__).resolve().parent
WWW = BASE / 'wwwroot'
DATA = BASE / 'Data'
ARTISTS = DATA / 'All Artists'
WIDGETS = DATA / 'Home Widgets'
PLAYLISTS = DATA / 'Playlists'
EVENTS = DATA / 'Events'
CLIPS = DATA / 'Clips'
CLIPS_CONFIG = CLIPS / 'clips.json'
ROOMS_DIR = DATA / 'Rooms'
EMOJIS_DIR = DATA / 'Emojis'
LYRICS_DIR = DATA / 'Lyrics'
ROOMS = {}
ROOM_LOCK = threading.RLock()
EVENTS_CONFIG = EVENTS / 'quests.json'
LIBRARY = DATA / 'user-library.json'
USERS = DATA / 'Users'
USERS_FILE = USERS / 'users.json'
SITE_SETTINGS_FILE = DATA / 'site-settings.json'
SESSIONS = {}
LOGIN_ATTEMPTS = {}
SESSION_LOCK = threading.RLock()
HOST = os.environ.get('INMUSIC_HOST', '127.0.0.1')
PORT = int(os.environ.get('INMUSIC_PORT', '5080'))
STARTED = time.time()
LOCK = threading.RLock()
MAINTENANCE_UNTIL = 0
SERVER_MESSAGE = ''
for folder in (ARTISTS, WIDGETS, PLAYLISTS, EVENTS, USERS, CLIPS, CLIPS / 'videos', CLIPS / 'covers', ROOMS_DIR, EMOJIS_DIR, LYRICS_DIR): folder.mkdir(parents=True, exist_ok=True)

# Any source-code edits show the restart overlay. Music and widget data remain live.
def sig(p):
    try:
        s=p.stat(); return (s.st_mtime_ns, s.st_size)
    except OSError: return None

def code_snapshot():
    found={str(BASE/'app.py'):sig(BASE/'app.py')}
    for p in WWW.rglob('*'):
        if p.is_file() and p.suffix.lower() in {'.js','.css','.html','.py'}: found[str(p)]=sig(p)
    return found

BASELINE=code_snapshot()
def restart_required(): return code_snapshot()!=BASELINE

def safe(parent, name):
    if parent is None: return None
    p=(parent/name).resolve()
    try: p.relative_to(parent.resolve()); return p
    except (ValueError,OSError): return None

def within(path, parent):
    try: path.resolve().relative_to(parent.resolve()); return True
    except (ValueError,OSError): return False

def read_json(path, fallback):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError,UnicodeError): return fallback

def write_json(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temp.replace(path)

def library(user_id=None):
    path = safe(USERS, str(user_id) + '-library.json') if user_id else LIBRARY
    with LOCK:
        d=read_json(path,{})
        if not isinstance(d,dict): d={}
        return {'favoriteArtists':list(dict.fromkeys(x for x in d.get('favoriteArtists',[]) if isinstance(x,str))),
                'favoriteSongs':list(dict.fromkeys(x for x in d.get('favoriteSongs',[]) if isinstance(x,str))),
                'subscribedArtists':list(dict.fromkeys(x for x in d.get('subscribedArtists',[]) if isinstance(x,str)))}

def save_library(d,user_id=None):
    path = safe(USERS, str(user_id) + '-library.json') if user_id else LIBRARY
    with LOCK: write_json(path,d)

def users_data():
    d=read_json(USERS_FILE,[])
    return d if isinstance(d,list) else []

def save_users(items): write_json(USERS_FILE,items)
def site_settings():
    d=read_json(SITE_SETTINGS_FILE, {})
    return {'eventsEnabled': bool(d.get('eventsEnabled', True))} if isinstance(d, dict) else {'eventsEnabled': True}
def save_site_settings(d): write_json(SITE_SETTINGS_FILE, {'eventsEnabled': bool(d.get('eventsEnabled', True))})
def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),salt,240000)
    return salt.hex()+':'+digest.hex()
def password_ok(password, encoded):
    try:
        salt_hex, digest_hex=encoded.split(':',1)
        actual=hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),bytes.fromhex(salt_hex),240000).hex()
        return hmac.compare_digest(actual,digest_hex)
    except Exception: return False
def public_user(u): return {'id':u['id'],'username':u['username'],'displayName':u.get('displayName') or u['username'],'role':u.get('role','user')}
def room_user(handler):
    u=get_session_user(handler)
    return {'id':u['id'],'name':u.get('displayName') or u['username']} if u else None
def clean_text(v,n=3000): return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',str(v or '')).strip()[:n]
def room_public(r):
    now=time.time(); vote=r.get('vote')
    if vote and now>=vote['endsAt']:
        counts={x:0 for x in vote['options']}
        for v in vote['votes'].values(): counts[v]=counts.get(v,0)+1
        winner=sorted(vote['options'],key=lambda x:(-counts.get(x,0),x))[0]
        r['queue'].append(winner); r['lastVote']={'winner':winner,'counts':counts}; r['vote']=None
    return {'id':r['id'],'name':r['name'],'locked':bool(r.get('passwordHash')),'members':[{'id':m['id'],'name':m['name'],'role':'Админ комнаты' if m['id']==r['ownerId'] else 'Слушатель'} for m in r['members'].values()], 'messages':r['messages'][-80:], 'vote':({'options':vote['options'],'endsAt':vote['endsAt'],'votesCount':len(vote['votes'])} if vote else None),'lastVote':r.get('lastVote'),'queue':r['queue'][-30:]}
def get_session_user(handler):
    cookie=handler.headers.get('Cookie','')
    token=''
    for part in cookie.split(';'):
        k,_,v=part.strip().partition('=')
        if k=='inmusic_session': token=v
    with SESSION_LOCK: uid=SESSIONS.get(token)
    if not uid: return None
    return next((u for u in users_data() if u.get('id')==uid),None)
def require_user(handler):
    u=get_session_user(handler)
    if not u: handler.json_out({'error':'Войдите в аккаунт, чтобы продолжить.'},401); return None
    return u
def require_role(handler, roles=('admin',)):
    u=require_user(handler)
    if not u: return None
    if u.get('role') not in roles: handler.json_out({'error':'Недостаточно прав.'},403); return None
    return u

def sortkey(s): return [int(x) if x.isdigit() else x.casefold() for x in re.split(r'(\d+)',s)]
def q(s): return quote(str(s),safe='')

def artist_dirs():
    return [p for p in sorted(ARTISTS.iterdir(),key=lambda x:sortkey(x.name)) if p.is_dir() and not p.name.startswith('.')]
def album_dirs(artist):
    d=artist/'Albums'
    return [p for p in sorted(d.iterdir(),key=lambda x:sortkey(x.name)) if p.is_dir() and not p.name.startswith('.')] if d.is_dir() else []
def single_dirs(artist):
    d=artist/'Singles'
    return [p for p in sorted(d.iterdir(),key=lambda x:sortkey(x.name)) if p.is_dir() and not p.name.startswith('.')] if d.is_dir() else []
def release_meta(d, kind='album'):
    m=read_json(d/('single.json' if kind=='single' else 'album.json'),{})
    if not isinstance(m,dict): m={}
    credits=str(m.get('credits') or '').strip()
    if (d/'credits.txt').is_file():
        try: credits=(d/'credits.txt').read_text(encoding='utf-8').strip()
        except OSError: pass
    collaborators=m.get('collaborators') or []
    if isinstance(collaborators,str): collaborators=[x.strip() for x in collaborators.split(',') if x.strip()]
    return {'title':str(m.get('title') or d.name),'duration':str(m.get('duration') or '—'),
            'year':str(m.get('year') or ''),'releaseDate':str(m.get('release_date') or m.get('releaseDate') or ''),
            'cover':str(m.get('cover') or 'cover.jpg'),'credits':credits,'collaborators':[str(x).strip() for x in collaborators if str(x).strip()],
            'gradientColor':str(m.get('gradient_color') or '#6b245f'),
            'gradientOpacity':max(0,min(100,int(m.get('gradient_opacity',88)))) if str(m.get('gradient_opacity',88)).lstrip('-').isdigit() else 88}
album_meta=release_meta

def songs_for(artist,album,favs=None,kind='album'):
    ad=safe(ARTISTS,artist); collection='Singles' if kind=='single' else 'Albums'
    bd=safe(ad/collection,album) if ad else None
    if not bd or not bd.is_dir(): return None
    sd=bd/'songs'
    if not sd.is_dir(): return []
    meta=release_meta(bd,kind)
    cover_path=safe(bd,meta['cover'])
    cover_url=(f'/media/covers/{q(artist)}/{collection}/{q(album)}/{q(cover_path.name)}' if kind=='single' and cover_path and cover_path.is_file() and cover_path.parent==bd.resolve() else
               f'/media/covers/{q(artist)}/{q(album)}/{q(cover_path.name)}' if cover_path and cover_path.is_file() and cover_path.parent==bd.resolve() else '')
    out=[]
    for f in sorted(sd.iterdir(),key=lambda x:sortkey(x.name)):
        if not f.is_file() or f.suffix.lower()!='.mp3' or f.name.startswith('.'): continue
        m=re.match(r'^\s*(\d{1,3})\s*[-._)]\s*(.+)$',f.stem)
        song_id=f'{artist}/Singles/{album}/{f.name}' if kind=='single' else f'{artist}/{album}/{f.name}'
        song_url=f'/media/songs/{q(artist)}/Singles/{q(album)}/{q(f.name)}' if kind=='single' else f'/media/songs/{q(artist)}/{q(album)}/{q(f.name)}'
        out.append({'id':song_id,'title':m.group(2).strip() if m else f.stem,
                    'trackNumber':int(m.group(1)) if m else None,'artist':artist,'album':meta['title'],
                    'releaseType':kind,'cover':cover_url,'url':song_url,
                    'isFavorite':song_id in (favs or set())})
    return out

def all_songs(user_id=None):
    favs=set(library(user_id)['favoriteSongs']) if user_id else set(); out=[]
    for artist in artist_dirs():
        for album in album_dirs(artist): out.extend(songs_for(artist.name,album.name,favs,'album') or [])
        for single in single_dirs(artist): out.extend(songs_for(artist.name,single.name,favs,'single') or [])
    return out

def event_quests():
    config=read_json(EVENTS_CONFIG, {})
    configured=config.get('quests',[]) if isinstance(config,dict) else []
    songs=all_songs()
    if isinstance(configured,list) and configured:
        quests=[]
        for item in configured:
            if not isinstance(item,dict): continue
            artist=str(item.get('artist') or '').strip()
            target=max(1,min(100,int(item.get('target',5)))) if str(item.get('target',5)).isdigit() else 5
            available=sum(1 for song in songs if song['artist']==artist)
            if artist and available:
                quests.append({'id':str(item.get('id') or ('artist-'+q(artist))),'title':str(item.get('title') or f'Музыкальный марафон: {artist}'),
                    'description':str(item.get('description') or f'Послушай {target} разных песен исполнителя {artist}.'),
                    'artist':artist,'target':target,'availableSongs':available,'type':'artist_listens'})
        return quests
    quests=[]
    by_artist={}
    for song in songs: by_artist.setdefault(song['artist'],[]).append(song)
    for artist,tracks in by_artist.items():
        if len(tracks)>=5 and len(quests)<3:
            quests.append({'id':'artist-'+q(artist),'title':f'Музыкальный марафон: {artist}',
                'description':f'Послушай 5 разных песен исполнителя {artist}.',
                'artist':artist,'target':5,'availableSongs':len(tracks),'type':'artist_listens'})
    return quests

def artist_image_url(ad):
    meta=read_json(ad/'artist.json',{})
    if not isinstance(meta,dict): meta={}
    image_name=str(meta.get('image') or meta.get('portrait') or meta.get('photo') or 'artist.png').strip()
    image=safe(ad,image_name) if image_name else None
    if image and image.is_file() and image.parent==ad.resolve() and image.suffix.lower() in {'.png','.jpg','.jpeg','.webp'}:
        return f'/media/artist-assets/{q(ad.name)}/{q(image.name)}'
    return ''

def artist_list(user_id=None):
    lib=library(user_id) if user_id else {'favoriteArtists':[],'subscribedArtists':[]}
    favs=set(lib['favoriteArtists']); subs=set(lib['subscribedArtists'])
    return [{'id':a.name,'name':a.name,'albumsCount':len(album_dirs(a)),'singlesCount':len(single_dirs(a)),'isFavorite':a.name in favs,'isSubscribed':a.name in subs,'image':artist_image_url(a)} for a in artist_dirs()]

def artist_detail(name):
    ad=safe(ARTISTS,name)
    if not ad or not ad.is_dir(): return None
    meta=read_json(ad/'artist.json',{})
    if not isinstance(meta,dict): meta={}
    bio=str(meta.get('biography') or meta.get('bio') or '').strip()
    if not bio and (ad/'biography.txt').is_file():
        try: bio=(ad/'biography.txt').read_text(encoding='utf-8').strip()
        except OSError: pass
    banner=str(meta.get('banner') or meta.get('background') or '').strip()
    banner_url=''
    if banner:
        image=safe(ad,banner)
        if image and image.is_file() and image.parent==ad.resolve():
            banner_url=f'/media/artist-assets/{q(name)}/{q(image.name)}'
    return {'name':name,'biography':bio,'banner':banner_url,'tagline':clean_text(meta.get('tagline'),120),'creator':clean_text(meta.get('creatorName') or 'Загружено сервисом',60),'albumsCount':len(album_dirs(ad)),'singlesCount':len(single_dirs(ad))}

def release_list(name,kind='album',user_id=None):
    ad=safe(ARTISTS,name)
    if not ad or not ad.is_dir(): return None
    favs=set(library(user_id)['favoriteSongs']) if user_id else set(); out=[]
    collection='Singles' if kind=='single' else 'Albums'
    dirs=single_dirs(ad) if kind=='single' else album_dirs(ad)
    for d in dirs:
        m=release_meta(d,kind); cp=safe(d,m['cover']); cover=''
        if cp and cp.is_file() and cp.parent==d.resolve():
            cover=(f'/media/covers/{q(name)}/Singles/{q(d.name)}/{q(cp.name)}' if kind=='single' else f'/media/covers/{q(name)}/{q(d.name)}/{q(cp.name)}')
        out.append({'id':d.name,'title':m['title'],'duration':m['duration'],'year':m['year'],'releaseDate':m['releaseDate'],'cover':cover,'gradientColor':m['gradientColor'],'gradientOpacity':m['gradientOpacity'],
                    'songsCount':len(songs_for(name,d.name,favs,kind) or []),'type':kind})
    return out

def album_list(name,user_id=None): return release_list(name,'album',user_id)

def release_detail(artist,release,user_id=None,kind='album'):
    ad=safe(ARTISTS,artist); collection='Singles' if kind=='single' else 'Albums'; d=safe(ad/collection,release) if ad else None
    if not d or not d.is_dir(): return None
    m=release_meta(d,kind); cp=safe(d,m['cover']); cover=''
    if cp and cp.is_file() and cp.parent==d.resolve():
        cover=(f'/media/covers/{q(artist)}/Singles/{q(release)}/{q(cp.name)}' if kind=='single' else f'/media/covers/{q(artist)}/{q(release)}/{q(cp.name)}')
    return {'artist':artist,'title':m['title'],'duration':m['duration'],'year':m['year'],'cover':cover,'type':kind,
            'credits':m['credits'],'gradientColor':m['gradientColor'],'gradientOpacity':m['gradientOpacity'],'songs':songs_for(artist,release,set(library(user_id)['favoriteSongs']) if user_id else set(),kind) or []}

def album_detail(artist,album,user_id=None): return release_detail(artist,album,user_id,'album')

def clip_list():
    config=read_json(CLIPS_CONFIG, [])
    items=config.get('clips',[]) if isinstance(config,dict) else config
    if not isinstance(items,list): items=[]
    songs={x['id']:x for x in all_songs()}
    out=[]
    for item in items:
        if not isinstance(item,dict): continue
        sid=str(item.get('songId') or item.get('song_id') or '').strip()
        song=songs.get(sid)
        filename=str(item.get('file') or '').strip()
        video=safe(CLIPS/'videos',filename) if filename else None
        if not song or not video or not video.is_file() or video.parent!=(CLIPS/'videos').resolve() or video.suffix.lower()!='.mp4': continue
        cover_name=str(item.get('cover') or '').strip()
        cover=safe(CLIPS/'covers',cover_name) if cover_name else None
        cover_url=f'/media/clip-covers/{q(cover.name)}' if cover and cover.is_file() and cover.parent==(CLIPS/'covers').resolve() else song.get('cover','')
        out.append({'id':str(item.get('id') or sid),'songId':sid,'title':str(item.get('title') or song['title']),
            'artist':song['artist'],'album':song['album'],'songTitle':song['title'],
            'video':f'/media/clips/{q(video.name)}','cover':cover_url,'song':song})
    return out

def widget_list(page=None,user_id=None):
    out=[]
    for d in sorted(WIDGETS.iterdir(),key=lambda x:sortkey(x.name)):
        if not d.is_dir() or d.name.startswith('.'): continue
        c=read_json(d/'widget.json',{})
        if not isinstance(c,dict) or c.get('enabled', True) is False: continue
        widget_page=str(c.get('page') or 'home').strip().lower()
        if page and widget_page != page: continue
        required_artist=str(c.get('show_when_subscribed') or '').strip()
        if required_artist and required_artist not in library(user_id)['subscribedArtists']: continue

        def media_url(value):
            if not value: return ''
            img=safe(d,str(value))
            if not img or not img.is_file() or not within(img,d): return ''
            assets=safe(d,'assets')
            # Prefer paths relative to assets because the media endpoint serves that folder.
            # Also allow widget files stored directly in the widget folder.
            base=assets if assets and assets.is_dir() and within(img,assets) else d
            return '/widget-media/'+q(d.name)+'/'+'/'.join(q(x) for x in img.relative_to(base.resolve()).parts)

        # New schema: background + artist_image. Keep legacy image for older widgets.
        background=media_url(c.get('background'))
        artist_image=media_url(c.get('artist_image') or c.get('artistImage'))
        legacy_image=media_url(c.get('image'))
        if not background and legacy_image:
            artist_image=legacy_image
        script=f'/widget-scripts/{q(d.name)}/widget.js' if (d/'widget.js').is_file() else ''
        try: order=int(c.get('order',100))
        except (ValueError,TypeError): order=100
        try: height=max(220,min(600,int(c.get('height',320))))
        except (ValueError,TypeError): height=320
        try: radius=max(0,min(48,int(c.get('border_radius',18))))
        except (ValueError,TypeError): radius=18
        requested_songs=c.get('songs',[])
        if not isinstance(requested_songs,list): requested_songs=[]
        song_catalog={song['id']:song for song in all_songs()}
        selected_songs=[song_catalog[sid] for sid in requested_songs if isinstance(sid,str) and sid in song_catalog]
        out.append({
            'page':widget_page,
            'id':str(c.get('id') or d.name),
            'type':str(c.get('type') or 'artist'),
            'title':str(c.get('title') or d.name),
            'artist':str(c.get('artist') or c.get('title') or ''),
            'description':str(c.get('description') or ''),
            'background':background,
            'artistImage':artist_image,
            'image':artist_image,
            'layout':str(c.get('layout') or 'text-left-image-right'),
            'height':height,
            'borderRadius':radius,
            'gradient':bool(c.get('gradient',True)),
            'gradientColor':str(c.get('gradient_color') or '#080808'),
            'gradientOpacity':max(0,min(100,int(c.get('gradient_opacity',96)))) if str(c.get('gradient_opacity',96)).lstrip('-').isdigit() else 96,
            'button':c.get('button') if isinstance(c.get('button'),dict) else None,
            'questId':str(c.get('quest_id') or ''),
            'songs':selected_songs,
            'clips':[x for x in clip_list() if not c.get('clip_id') or x['id']==str(c.get('clip_id'))],
            'clipId':str(c.get('clip_id') or ''),
            'previewSeconds':max(5,min(30,int(c.get('preview_seconds',30)))) if str(c.get('preview_seconds',30)).isdigit() else 30,
            'sideClip':str(c.get('side_clip') or ''),
            'scriptUrl':script,
            'order':order
        })
    return sorted(out,key=lambda x:(x['order'],sortkey(x['id'])))

def data_snapshot():
    # Fingerprint editable content/configuration; excludes transient session and cache files.
    rows=[]
    for folder in (DATA,):
        for item in folder.rglob('*'):
            if not item.is_file() or any(part.startswith('.') for part in item.relative_to(folder).parts): continue
            try:
                st=item.stat(); rows.append((str(item.relative_to(folder)),st.st_mtime_ns,st.st_size))
            except OSError: pass
    raw=json.dumps(rows,separators=(',',':')).encode()
    return hashlib.sha256(raw).hexdigest()

def playlists_list(user_id=None):
    out=[]
    for p in sorted(PLAYLISTS.glob('*.json')):
        d=read_json(p,{})
        if isinstance(d,dict) and (user_id is None or d.get('ownerId')==user_id): out.append({'id':str(d.get('id') or p.stem),'name':str(d.get('name') or 'Untitled'),
                                            'songsCount':len(d.get('songs',[])),'createdAt':str(d.get('createdAt') or '')})
    return out

def playlist_detail(pid,user_id=None):
    p=safe(PLAYLISTS,pid+'.json')
    if not p or not p.is_file(): return None
    d=read_json(p,{})
    if not isinstance(d,dict) or (user_id is not None and d.get('ownerId')!=user_id): return None
    catalog={s['id']:s for s in all_songs(user_id)}
    return {'id':str(d.get('id') or pid),'name':str(d.get('name') or 'Untitled'),
            'createdAt':str(d.get('createdAt') or ''),'songs':[catalog[x] for x in d.get('songs',[]) if x in catalog]}

def local_only(h): return h.client_address[0] in ('127.0.0.1','::1') or h.client_address[0].startswith('127.')

class Handler(SimpleHTTPRequestHandler):
    server_version='InMusic/1.16'
    def __init__(self,*a,**kw): super().__init__(*a,directory=str(WWW),**kw)
    def end_headers(self):
        self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); super().end_headers()
    def json_out(self,data,status=200):
        b=json.dumps(data,ensure_ascii=False).encode(); self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0') or 0)
        if n<0 or n>1000000: raise ValueError('Request body too large')
        d=json.loads(self.rfile.read(n).decode() if n else '{}')
        if not isinstance(d,dict): raise ValueError('JSON object expected')
        return d
    def multipart(self, limit=60*1024*1024):
        n=int(self.headers.get('Content-Length','0') or 0)
        if n<=0 or n>limit: raise ValueError('Недопустимый размер загрузки (максимум 60 МБ).')
        raw=self.rfile.read(n)
        msg=BytesParser(policy=default).parsebytes((f'Content-Type: {self.headers.get("Content-Type","")}\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
        fields={}; files={}
        for part in msg.iter_parts():
            name=part.get_param('name',header='content-disposition')
            if not name: continue
            data=part.get_payload(decode=True) or b''
            filename=part.get_filename()
            if filename: files[name]={'filename':Path(filename).name,'content':data,'type':part.get_content_type()}
            else: fields[name]=data.decode('utf-8','replace')
        return fields,files
    def media(self,kind,raw):
        parts=[unquote(x) for x in raw if x]
        try:
            if kind=='songs' and len(parts) in (3,4):
                if len(parts)==4 and parts[1]=='Singles':
                    a,_,release,fn=parts; ad=safe(ARTISTS,a); bd=safe(ad/'Singles',release) if ad else None
                else:
                    a,release,fn=parts; ad=safe(ARTISTS,a); bd=safe(ad/'Albums',release) if ad else None
                sd=safe(bd,'songs') if bd else None; f=safe(sd,fn) if sd else None
                valid=f and f.is_file() and f.parent==sd.resolve() and f.suffix.lower()=='.mp3'
            elif kind=='clips' and len(parts)==1:
                f=safe(CLIPS/'videos',parts[0]); valid=f and f.is_file() and f.parent==(CLIPS/'videos').resolve() and f.suffix.lower()=='.mp4'
            elif kind=='clip-covers' and len(parts)==1:
                f=safe(CLIPS/'covers',parts[0]); valid=f and f.is_file() and f.parent==(CLIPS/'covers').resolve() and f.suffix.lower() in {'.png','.jpg','.jpeg','.webp'}
            elif kind=='artist-assets' and len(parts)==2:
                a,fn=parts; ad=safe(ARTISTS,a); f=safe(ad,fn) if ad else None
                valid=f and f.is_file() and f.parent==ad.resolve() and f.suffix.lower() in {'.png','.jpg','.jpeg','.webp'}
            elif kind=='covers' and len(parts) in (3,4):
                if len(parts)==4 and parts[1]=='Singles':
                    a,_,release,fn=parts; ad=safe(ARTISTS,a); bd=safe(ad/'Singles',release) if ad else None
                else:
                    a,release,fn=parts; ad=safe(ARTISTS,a); bd=safe(ad/'Albums',release) if ad else None
                f=safe(bd,fn) if bd else None
                valid=f and f.is_file() and f.parent==bd.resolve()
            elif kind=='widgets' and len(parts)>=2:
                wd=safe(WIDGETS,parts[0])
                assets=safe(wd,'assets') if wd else None
                # First resolve the canonical assets-relative URL; fall back to a
                # widget-root-relative URL for older widget packages.
                f=safe(assets,'/'.join(parts[1:])) if assets else None
                valid=f and f.is_file() and within(f,assets)
                if not valid and wd:
                    f=safe(wd,'/'.join(parts[1:]))
                    valid=f and f.is_file() and within(f,wd)
            elif kind=='emojis' and len(parts)==1:
                f=safe(EMOJIS_DIR,parts[0]); valid=f and f.is_file() and f.parent==EMOJIS_DIR.resolve() and f.suffix.lower()=='.png'
            elif kind=='roles' and len(parts)==2:
                rd=safe(BASE/'Data'/'Roles',parts[0]); f=safe(rd,parts[1]) if rd else None
                valid=f and f.is_file() and f.parent==rd.resolve() and f.suffix.lower()=='.png'
            elif kind=='widget-scripts' and len(parts)==2 and parts[1]=='widget.js':
                wd=safe(WIDGETS,parts[0]); f=safe(wd,'widget.js') if wd else None
                valid=f and f.is_file() and f.parent==wd.resolve()
            else: return self.send_error(404)
            if not valid: return self.send_error(404)
            size=f.stat().st_size; start=0; end=max(size-1,0); partial=False
            rh=self.headers.get('Range','')
            if rh.startswith('bytes=') and size:
                m=re.fullmatch(r'(\d*)-(\d*)',rh[6:].split(',',1)[0].strip())
                if m:
                    left,right=m.groups()
                    if left: start=int(left); end=int(right) if right else size-1
                    elif right: start=max(0,size-int(right)); end=size-1
                    if start> end or start>=size:
                        self.send_response(416); self.send_header('Content-Range',f'bytes */{size}'); self.end_headers(); return
                    end=min(end,size-1); partial=True
            length=max(0,end-start+1); self.send_response(206 if partial else 200)
            self.send_header('Content-Type',mimetypes.guess_type(str(f))[0] or 'application/octet-stream')
            self.send_header('Accept-Ranges','bytes'); self.send_header('Content-Length',str(length))
            if partial: self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
            self.end_headers()
            with f.open('rb') as stream:
                stream.seek(start); remaining=length
                while remaining:
                    chunk=stream.read(min(65536,remaining))
                    if not chunk: break
                    self.wfile.write(chunk); remaining-=len(chunk)
        except (OSError,ValueError): self.send_error(404)
    def do_GET(self):
        global MAINTENANCE_UNTIL
        raw=urlparse(self.path).path; path=unquote(raw)
        if MAINTENANCE_UNTIL and time.time() >= MAINTENANCE_UNTIL: MAINTENANCE_UNTIL=0
        if MAINTENANCE_UNTIL and not path.startswith('/api/admin/') and path not in ('/api/restart-status','/api/server-message'):
            return self.json_out({'error':'InMusic временно на обслуживании. Попробуйте позже.'},503)
        if path=='/api/lyrics':
            from urllib.parse import parse_qs
            params=parse_qs(urlparse(self.path).query)
            artist=clean_text(params.get('artist',[''])[0],70)
            title=clean_text(params.get('title',[''])[0],120)
            album=clean_text(params.get('album',[''])[0],90)
            def lyric_filename(value):
                value=re.sub(r'[\\x00-\\x1f/\\\\]+','_',value).strip(' .')[:120]
                return value or 'untitled'
            artist_dir=safe(LYRICS_DIR,lyric_filename(artist))
            lyric_file=safe(safe(artist_dir,lyric_filename(album)),lyric_filename(title)+'.lrc') if artist_dir and album else None
            if not lyric_file or not lyric_file.is_file():
                lyric_file=safe(artist_dir,lyric_filename(title)+'.lrc') if artist_dir else None
            if not lyric_file: return self.json_out({'error':'Некорректное имя песни.'},400)
            try: content=lyric_file.read_text(encoding='utf-8-sig') if lyric_file.is_file() else ''
            except OSError: content=''
            return self.json_out({'lyrics':content,'filename':lyric_file.name})
        if path=='/api/site-settings':
            return self.json_out(site_settings())
        if path=='/api/auth/me':
            u=get_session_user(self); return self.json_out({'user':public_user(u) if u else None,'registrationOpen':True})
        if path=='/api/admin/users':
            if not require_role(self): return
            return self.json_out({'users':[public_user(u) for u in users_data()]})
        if path=='/api/server-message': return self.json_out({'message':SERVER_MESSAGE})
        if path=='/api/admin/status':
            if not require_role(self): return
            aa=artist_dirs()
            return self.json_out({'running':True,'pid':os.getpid(),'uptimeSeconds':int(time.time()-STARTED),'artists':len(aa),'albums':sum(len(album_dirs(a)) for a in aa),'songs':len(all_songs()),'widgets':len(widget_list()),'playlists':len(playlists_list()),'maintenanceUntil':MAINTENANCE_UNTIL,'serverMessage':SERVER_MESSAGE})
        if path=='/api/content/emoji':
            return self.json_out({'emojis':[{'id':f.stem,'url':'/media/emojis/'+q(f.name)} for f in sorted(EMOJIS_DIR.iterdir(),key=lambda x:sortkey(x.name)) if f.is_file() and f.suffix.lower()=='.png' and not f.name.startswith('.') ]})
        if path=='/api/rooms':
            with ROOM_LOCK: return self.json_out({'rooms':[{'id':r['id'],'name':r['name'],'locked':bool(r.get('passwordHash')),'members':len(r['members'])} for r in ROOMS.values()]})
        if path.startswith('/api/rooms/'):
            rid=path.split('/')[3]
            with ROOM_LOCK:
                r=ROOMS.get(rid)
                if not r: return self.json_out({'error':'Комната не найдена.'},404)
                me=room_user(self)
                if not me or me['id'] not in r['members']: return self.json_out({'error':'Сначала войдите в комнату.'},403)
                r['members'][me['id']]['seenAt']=time.time()
                return self.json_out(room_public(r))
        if path=='/api/artists': return self.json_out({'artists':artist_list((get_session_user(self) or {}).get('id'))})
        if path.startswith('/api/artists/') and path.endswith('/detail'):
            name=unquote(raw[len('/api/artists/'):-len('/detail')].strip('/')); d=artist_detail(name)
            return self.json_out(d) if d else self.json_out({'error':'Artist not found'},404)
        if path=='/api/songs': return self.json_out({'songs':all_songs((get_session_user(self) or {}).get('id'))})
        if path=='/api/clips': return self.json_out({'clips':clip_list()})
        if path=='/api/favorites':
            u=get_session_user(self); return self.json_out(library(u['id']) if u else {'favoriteArtists':[],'favoriteSongs':[],'subscribedArtists':[]})
        if path=='/api/subscriptions/artists':
            u=require_user(self)
            if not u: return
            return self.json_out({'artists':library(u['id'])['subscribedArtists']})
        if path=='/api/home-widgets':
            from urllib.parse import parse_qs
            requested_page=parse_qs(urlparse(self.path).query).get('page',['home'])[0].strip().lower()
            if requested_page not in ('home','songs','playlists','events','clips'): requested_page='home'
            return self.json_out({'widgets':widget_list(requested_page,(get_session_user(self) or {}).get('id'))})
        if path=='/api/live-version': return self.json_out({'version':data_snapshot()})
        if path=='/api/events/quests':
            if not site_settings()['eventsEnabled']: return self.json_out({'error':'Вкладка «Ивенты» временно отключена.'},404)
            return self.json_out({'quests':event_quests()})
        if path=='/api/playlists':
            u=get_session_user(self); return self.json_out({'playlists':playlists_list(u['id']) if u else []})
        if path.startswith('/api/playlists/'):
            u=get_session_user(self)
            if not u: return self.json_out({'error':'Войдите в аккаунт.'},401)
            d=playlist_detail(path[len('/api/playlists/'):].strip('/'),u['id'])
            return self.json_out(d) if d else self.json_out({'error':'Playlist not found'},404)
        if path.startswith('/api/artists/') and path.endswith('/discography'):
            name=unquote(raw[len('/api/artists/'):-len('/discography')].strip('/')); uid=(get_session_user(self) or {}).get('id')
            albums=release_list(name,'album',uid); singles=release_list(name,'single',uid); collaborations=[]
            if albums is not None:
                for other in artist_dirs():
                    if other.name==name: continue
                    for kind, folders in (('album',album_dirs(other)),('single',single_dirs(other))):
                        for folder in folders:
                            meta=release_meta(folder,kind)
                            if name in meta.get('collaborators',[]):
                                collaborations.append({'artist':other.name,'title':meta['title'],'release':folder.name,'type':kind,'collaborators':meta.get('collaborators',[])})
            return self.json_out({'artist':name,'albums':albums,'singles':singles,'collaborations':collaborations}) if albums is not None else self.json_out({'error':'Artist not found'},404)
        if path.startswith('/api/releases/'):
            parts=raw[len('/api/releases/'):].split('/')
            if len(parts)==3 and unquote(parts[1]) in ('album','single'):
                d=release_detail(unquote(parts[0]),unquote(parts[2]),(get_session_user(self) or {}).get('id'),unquote(parts[1]))
                return self.json_out(d) if d else self.json_out({'error':'Release not found'},404)
        if path.startswith('/api/artists/') and path.endswith('/albums'):
            name=unquote(raw[len('/api/artists/'):-len('/albums')].strip('/')); d=album_list(name,(get_session_user(self) or {}).get('id'))
            return self.json_out({'artist':name,'albums':d}) if d is not None else self.json_out({'error':'Artist not found'},404)
        if path.startswith('/api/albums/'):
            parts=raw[len('/api/albums/'):].split('/')
            if len(parts)==2:
                d=album_detail(unquote(parts[0]),unquote(parts[1]),(get_session_user(self) or {}).get('id')); return self.json_out(d) if d else self.json_out({'error':'Album not found'},404)
        if path=='/api/restart-status': return self.json_out({'restartRequired':restart_required()})
        if path=='/api/developer/status':
            if not require_role(self,('admin','moderator')): return
            aa=artist_dirs()
            return self.json_out({'running':True,'pid':os.getpid(),'python':sys.version.split()[0],
                'uptimeSeconds':int(time.time()-STARTED),'artists':len(aa),'albums':sum(len(album_dirs(a)) for a in aa),
                'songs':len(all_songs()),'widgets':len(widget_list()),'playlists':len(playlists_list()),
                'restartRequired':restart_required(),'root':str(BASE),'artistsPath':str(ARTISTS),'widgetsPath':str(WIDGETS)})
        if path.startswith('/media/songs/'): return self.media('songs',raw[len('/media/songs/'):].split('/'))
        if path.startswith('/media/covers/'): return self.media('covers',raw[len('/media/covers/'):].split('/'))
        if path.startswith('/media/clips/'): return self.media('clips',raw[len('/media/clips/'):].split('/'))
        if path.startswith('/media/clip-covers/'): return self.media('clip-covers',raw[len('/media/clip-covers/'):].split('/'))
        if path.startswith('/media/artist-assets/'): return self.media('artist-assets',raw[len('/media/artist-assets/'):].split('/'))
        if path.startswith('/widget-media/'): return self.media('widgets',raw[len('/widget-media/'):].split('/'))
        if path.startswith('/widget-scripts/'): return self.media('widget-scripts',raw[len('/widget-scripts/'):].split('/'))
        if path.startswith('/roles/'): return self.media('roles',raw[len('/roles/'):].split('/'))
        if path.startswith('/media/emojis/'): return self.media('emojis',raw[len('/media/emojis/'):].split('/'))
        if path in ('/','/InMusic','/InMusic/') or path.startswith('/InMusic/'):
            self.path='/index.html'; return super().do_GET()
        return super().do_GET()
    def do_POST(self):
        global MAINTENANCE_UNTIL, SERVER_MESSAGE
        path=unquote(urlparse(self.path).path)
        if path=='/api/content/upload':
            user=require_role(self,('admin','content_master'))
            if not user: return
            try: fields,files=self.multipart()
            except Exception as e: return self.json_out({'error':str(e)},400)
            up=files.get('file')
            if not up or not up['content']: return self.json_out({'error':'Выбери файл.'},400)
            artist=clean_text(fields.get('artist'),70); album=clean_text(fields.get('album'),90); kind=fields.get('kind','')
            ad=safe(ARTISTS,artist)
            if not ad or not ad.is_dir(): return self.json_out({'error':'Артист не найден.'},404)
            if kind=='song':
                bd=safe(ad/'Albums',album)
                if not bd or not bd.is_dir(): return self.json_out({'error':'Альбом не найден.'},404)
                if Path(up['filename']).suffix.lower()!='.mp3': return self.json_out({'error':'Для песен разрешён только MP3.'},400)
                target_dir=bd/'songs'; target_dir.mkdir(parents=True,exist_ok=True); ext='.mp3'
            elif kind in ('artist-image','album-cover'):
                ext=Path(up['filename']).suffix.lower()
                if ext not in ('.png','.jpg','.jpeg','.webp'): return self.json_out({'error':'Изображение должно быть PNG, JPG или WEBP.'},400)
                if kind=='album-cover':
                    bd=safe(ad/'Albums',album)
                    if not bd or not bd.is_dir(): return self.json_out({'error':'Альбом не найден.'},404)
                    target_dir=bd
                else: target_dir=ad
            else: return self.json_out({'error':'Неизвестный тип файла.'},400)
            base=re.sub(r'[^\w .()_-]+','_',Path(up['filename']).stem,flags=re.UNICODE).strip(' .')[:90] or 'upload'
            filename=(('cover' if kind=='album-cover' else 'artist')+ext) if kind!='song' else base+ext
            target=safe(target_dir,filename)
            if not target: return self.json_out({'error':'Недопустимое имя файла.'},400)
            if kind=='song' and target.exists(): return self.json_out({'error':'Файл с таким названием уже есть в альбоме.'},409)
            target.write_bytes(up['content'])
            if kind=='artist-image':
                meta=read_json(ad/'artist.json',{}); meta['image']=filename; write_json(ad/'artist.json',meta)
            elif kind=='album-cover':
                meta=read_json(target_dir/'album.json',{}); meta['cover']=filename; write_json(target_dir/'album.json',meta)
            return self.json_out({'ok':True,'filename':filename,'url':('/media/songs/'+q(artist)+'/'+q(album)+'/'+q(filename)) if kind=='song' else (('/media/covers/'+q(artist)+'/'+q(album)+'/'+q(filename)) if kind=='album-cover' else '/media/artist-assets/'+q(artist)+'/'+q(filename))},201)
        try: d=self.body()
        except (ValueError,UnicodeError,json.JSONDecodeError) as e: return self.json_out({'error':str(e)},400)
        if path=='/api/lyrics':
            artist=clean_text(d.get('artist'),70); title=clean_text(d.get('title'),120); album=clean_text(d.get('album'),90)
            content=str(d.get('lyrics') or '')
            if not artist or not title or len(content)>300000:
                return self.json_out({'error':'Проверь исполнителя, название и размер текста.'},400)
            def lyric_filename(value):
                value=re.sub(r'[\\x00-\\x1f/\\\\]+','_',value).strip(' .')[:120]
                return value or 'untitled'
            artist_dir=safe(LYRICS_DIR,lyric_filename(artist))
            if not artist_dir: return self.json_out({'error':'Некорректное имя исполнителя.'},400)
            artist_dir.mkdir(parents=True,exist_ok=True)
            target_dir=safe(artist_dir,lyric_filename(album)) if album else artist_dir
            if not target_dir: return self.json_out({'error':'Некорректное имя альбома.'},400)
            target_dir.mkdir(parents=True,exist_ok=True)
            target=safe(target_dir,lyric_filename(title)+'.lrc')
            if not target: return self.json_out({'error':'Некорректное имя песни.'},400)
            temp=target.with_name(target.name+'.tmp')
            temp.write_text(content,encoding='utf-8')
            temp.replace(target)
            return self.json_out({'ok':True,'filename':target.name,'path':str(target.relative_to(DATA))})
        if path=='/api/content/albums':
            user=require_role(self,('admin','content_master'))
            if not user: return
            artist=clean_text(d.get('artist'),70); title=clean_text(d.get('title'),90); ad=safe(ARTISTS,artist)
            if not ad or not ad.is_dir(): return self.json_out({'error':'Артист не найден.'},404)
            if not title or title in ('.','..') or '/' in title or '\\' in title: return self.json_out({'error':'Укажи корректное название альбома.'},400)
            bd=safe(ad/'Albums',title)
            if not bd: return self.json_out({'error':'Недопустимое название.'},400)
            if bd.exists(): return self.json_out({'error':'Такой альбом уже существует.'},409)
            color=str(d.get('gradientColor') or '#6b245f').strip()
            if not re.fullmatch(r'#[0-9a-fA-F]{6}',color): return self.json_out({'error':'Цвет должен быть в формате #RRGGBB.'},400)
            bd.mkdir(parents=True); (bd/'songs').mkdir()
            raw_collabs=d.get('collaborators') or []
            if isinstance(raw_collabs,str): raw_collabs=[x.strip() for x in raw_collabs.split(',') if x.strip()]
            known={p.name for p in artist_dirs()}; collaborators=[]
            for cname in raw_collabs:
                cname=clean_text(cname,70)
                if cname and cname!=artist and cname in known and cname not in collaborators: collaborators.append(cname)
            meta={'title':title,'year':clean_text(d.get('year'),4),'duration':clean_text(d.get('duration'),20) or '—','cover':'cover.jpg','gradient_color':color.lower(),'gradient_opacity':88,'credits':clean_text(d.get('credits'),2000),'collaborators':collaborators}
            write_json(bd/'album.json',meta)
            return self.json_out({'artist':artist,'album':title},201)
        if path=='/api/content/artists/tagline':
            user=require_role(self,('admin','content_master'))
            if not user: return
            name=clean_text(d.get('name'),70); ad=safe(ARTISTS,name)
            if not ad or not ad.is_dir(): return self.json_out({'error':'Артист не найден.'},404)
            meta=read_json(ad/'artist.json',{}); meta['tagline']=clean_text(d.get('tagline'),120); write_json(ad/'artist.json',meta)
            return self.json_out({'name':name,'tagline':meta['tagline']})
        if path=='/api/content/artists':
            user=require_role(self,('admin','content_master'))
            if not user: return
            name=clean_text(d.get('name'),70)
            if not name or name in ('.','..') or '/' in name or '\\' in name: return self.json_out({'error':'Укажи корректное имя артиста.'},400)
            ad=safe(ARTISTS,name)
            if not ad: return self.json_out({'error':'Недопустимое имя.'},400)
            with LOCK:
                if ad.exists(): return self.json_out({'error':'Такой артист уже существует.'},409)
                ad.mkdir(parents=True); (ad/'Albums').mkdir(); meta={'biography':clean_text(d.get('biography'),4000),'tagline':clean_text(d.get('tagline'),120),'creatorId':user['id'],'creatorName':user.get('displayName') or user['username'],'createdAt':datetime.now(timezone.utc).isoformat(),'image':'artist.png'}
                write_json(ad/'artist.json',meta)
            return self.json_out({'artist':name,'creator':meta['creatorName']},201)
        if path=='/api/rooms':
            me=room_user(self)
            if not me: return self.json_out({'error':'Войдите в аккаунт, чтобы создать комнату.'},401)
            name=clean_text(d.get('name'),50) or 'Музыкальная комната'; password=str(d.get('password') or '')
            if len(password)>100: return self.json_out({'error':'Слишком длинный пароль.'},400)
            rid=uuid.uuid4().hex[:12]
            with ROOM_LOCK:
                ROOMS[rid]={'id':rid,'name':name,'ownerId':me['id'],'members':{me['id']:{**me,'seenAt':time.time()}},'passwordHash':password_hash(password) if password else '', 'messages':[],'queue':[],'vote':None,'lastVote':None}
                write_json(ROOMS_DIR/(rid+'.json'),{'id':rid,'name':name,'ownerId':me['id'],'createdAt':datetime.now(timezone.utc).isoformat(),'messages':[]})
            return self.json_out({'id':rid},201)
        if path.startswith('/api/rooms/'):
            parts=path.strip('/').split('/'); rid=parts[2] if len(parts)>2 else ''
            me=room_user(self)
            if not me: return self.json_out({'error':'Войдите в аккаунт.'},401)
            with ROOM_LOCK:
                r=ROOMS.get(rid)
                if not r: return self.json_out({'error':'Комната не найдена.'},404)
                action=parts[3] if len(parts)>3 else ''
                if action=='join':
                    pw=str(d.get('password') or '')
                    if r['passwordHash'] and not password_ok(pw,r['passwordHash']): return self.json_out({'error':'Неверный пароль.'},403)
                    r['members'][me['id']]={**me,'seenAt':time.time()}; return self.json_out(room_public(r))
                if me['id'] not in r['members']: return self.json_out({'error':'Сначала войдите в комнату.'},403)
                r['members'][me['id']]['seenAt']=time.time()
                if action=='chat':
                    msg=clean_text(d.get('text'),240)
                    emoji=clean_text(d.get('emoji'),60)
                    if emoji and not (EMOJIS_DIR/(emoji+'.png')).is_file(): emoji=''
                    if not msg and not emoji: return self.json_out({'error':'Сообщение пустое.'},400)
                    r['messages'].append({'user':me['name'],'text':msg,'emoji':emoji,'time':datetime.now().strftime('%H:%M')}); r['messages']=r['messages'][-200:]
                    write_json(ROOMS_DIR/(rid+'.json'),{'name':r['name'],'messages':r['messages']})
                    return self.json_out({'ok':True})
                if action=='vote':
                    if me['id']!=r['ownerId']: return self.json_out({'error':'Голосование запускает только создатель комнаты.'},403)
                    song=clean_text(d.get('song'),300)
                    catalog={x['id']:x for x in all_songs()}
                    if song not in catalog: return self.json_out({'error':'Выбери песню из библиотеки.'},400)
                    r['vote']={'options':[song], 'votes':{}, 'endsAt':time.time()+15}; return self.json_out({'ok':True})
                if action=='cast':
                    v=r.get('vote'); choice=clean_text(d.get('song'),180)
                    if not v or time.time()>=v['endsAt'] or choice not in v['options']: return self.json_out({'error':'Голосование уже завершено.'},400)
                    v['votes'][me['id']]=choice; return self.json_out({'ok':True})
            return self.json_out({'error':'Неизвестное действие.'},404)
        if path=='/api/auth/register':
            username=str(d.get('username','')).strip()
            password=str(d.get('password',''))
            display=str(d.get('displayName') or username).strip()[:40]
            if not re.fullmatch(r'[A-Za-z0-9_]{3,24}',username): return self.json_out({'error':'Логин: 3–24 символа, латиница, цифры и _.'},400)
            if len(password)<10 or len(password)>128: return self.json_out({'error':'Пароль должен содержать от 10 до 128 символов.'},400)
            with LOCK:
                items=users_data()
                if any(x.get('username','').casefold()==username.casefold() for x in items): return self.json_out({'error':'Такой логин уже занят.'},409)
                role='admin' if not items else 'user'
                u={'id':uuid.uuid4().hex,'username':username,'displayName':display,'role':role,'passwordHash':password_hash(password),'createdAt':datetime.now(timezone.utc).isoformat()}
                items.append(u); save_users(items)
                if role=='admin':
                    # Preserve the previous single-user library when upgrading.
                    legacy=read_json(LIBRARY,{})
                    if isinstance(legacy,dict) and not safe(USERS,u['id']+'-library.json').exists():
                        write_json(safe(USERS,u['id']+'-library.json'),legacy)
                    for playlist_file in PLAYLISTS.glob('*.json'):
                        old_playlist=read_json(playlist_file,{})
                        if isinstance(old_playlist,dict) and not old_playlist.get('ownerId'):
                            old_playlist['ownerId']=u['id']; write_json(playlist_file,old_playlist)
            token=secrets.token_urlsafe(32)
            with SESSION_LOCK: SESSIONS[token]=u['id']
            self.send_response(201); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Set-Cookie',f'inmusic_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000'+('; Secure' if os.environ.get('INMUSIC_COOKIE_SECURE')=='1' else '')); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(json.dumps({'user':public_user(u)},ensure_ascii=False).encode()); return
        if path=='/api/auth/login':
            ip=self.client_address[0]; now=time.time(); attempts=[t for t in LOGIN_ATTEMPTS.get(ip,[]) if now-t<900]
            if len(attempts)>=8: return self.json_out({'error':'Слишком много попыток. Подождите 15 минут.'},429)
            username=str(d.get('username','')).strip(); password=str(d.get('password',''))
            u=next((x for x in users_data() if x.get('username','').casefold()==username.casefold()),None)
            if not u or not password_ok(password,u.get('passwordHash','')):
                attempts.append(now); LOGIN_ATTEMPTS[ip]=attempts; return self.json_out({'error':'Неверный логин или пароль.'},401)
            LOGIN_ATTEMPTS.pop(ip,None)
            token=secrets.token_urlsafe(32)
            with SESSION_LOCK: SESSIONS[token]=u['id']
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Set-Cookie',f'inmusic_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000'+('; Secure' if os.environ.get('INMUSIC_COOKIE_SECURE')=='1' else '')); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(json.dumps({'user':public_user(u)},ensure_ascii=False).encode()); return
        if path=='/api/auth/logout':
            cookie=self.headers.get('Cookie',''); token=''
            for part in cookie.split(';'):
                k,_,v=part.strip().partition('=')
                if k=='inmusic_session': token=v
            with SESSION_LOCK: SESSIONS.pop(token,None)
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Set-Cookie','inmusic_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'); self.end_headers(); self.wfile.write(b'{"ok":true}'); return
        if path=='/api/admin/site-settings':
            if not require_role(self): return
            save_site_settings(d)
            return self.json_out(site_settings())
        if path=='/api/admin/users/role':
            if not require_role(self): return
            uid=str(d.get('userId','')); role=str(d.get('role',''))
            if role not in ('user','moderator','admin','content_master'): return self.json_out({'error':'Неизвестная роль.'},400)
            current=get_session_user(self)
            with LOCK:
                items=users_data(); target=next((x for x in items if x.get('id')==uid),None)
                if not target: return self.json_out({'error':'Пользователь не найден.'},404)
                if target['id']==current['id'] and role!='admin': return self.json_out({'error':'Нельзя снять роль администратора с самого себя.'},400)
                target['role']=role; save_users(items)
            return self.json_out({'user':public_user(target)})
        if path.startswith('/api/admin/') and not require_role(self): return
        if path.startswith('/api/developer/') and not require_role(self,('admin','moderator')): return

        if path=='/api/admin/maintenance':
            minutes=max(0,min(60,int(d.get('minutes',0))))
            MAINTENANCE_UNTIL=time.time()+minutes*60 if minutes else 0
            return self.json_out({'maintenanceUntil':MAINTENANCE_UNTIL,'enabled':bool(minutes)})
        if path=='/api/admin/message':
            SERVER_MESSAGE=str(d.get('message','')).strip()[:300]
            return self.json_out({'message':SERVER_MESSAGE})
        if path=='/api/admin/rescan':
            aa=artist_dirs(); return self.json_out({'artists':len(aa),'albums':sum(len(album_dirs(a)) for a in aa),'songs':len(all_songs()),'widgets':len(widget_list()),'playlists':len(playlists_list())})
        if path=='/api/admin/restart':
            self.json_out({'message':'Restart requested.'})
            def do_restart():
                time.sleep(.8); os.execv(sys.executable,[sys.executable,str(BASE/'app.py')])
            threading.Thread(target=do_restart,daemon=True).start(); return
        if path=='/api/subscriptions/artist':
            user=require_user(self)
            if not user: return
            name=str(d.get('artist','')).strip(); artist_dir=safe(ARTISTS,name)
            if not name or not artist_dir or not artist_dir.is_dir(): return self.json_out({'error':'Artist not found'},404)
            lib=library(user['id']); subscribed=set(lib['subscribedArtists'])
            subscribed.add(name) if d.get('subscribe') else subscribed.discard(name)
            lib['subscribedArtists']=sorted(subscribed,key=sortkey); save_library(lib,user['id'])
            return self.json_out({'subscribed':name in subscribed})
        if path=='/api/favorites/artist':
            user=require_user(self)
            if not user: return
            name=str(d.get('artist','')).strip(); a=safe(ARTISTS,name)
            if not name or not a or not a.is_dir(): return self.json_out({'error':'Artist not found'},404)
            lib=library(user['id']); fav=set(lib['favoriteArtists']); fav.add(name) if d.get('favorite') else fav.discard(name)
            lib['favoriteArtists']=sorted(fav,key=sortkey); save_library(lib,user['id']); return self.json_out({'favorite':name in fav})
        if path=='/api/favorites/song':
            user=require_user(self)
            if not user: return
            sid=str(d.get('songId','')); valid={s['id'] for s in all_songs()}
            if sid not in valid: return self.json_out({'error':'Song not found'},404)
            lib=library(user['id']); fav=set(lib['favoriteSongs']); fav.add(sid) if d.get('favorite') else fav.discard(sid)
            lib['favoriteSongs']=sorted(fav); save_library(lib,user['id']); return self.json_out({'favorite':sid in fav})
        if path=='/api/playlists':
            user=require_user(self)
            if not user: return
            name=str(d.get('name','')).strip()
            if not name or len(name)>80: return self.json_out({'error':'Enter a playlist name (up to 80 characters).'},400)
            pid=uuid.uuid4().hex; fav=set(library(user['id'])['favoriteSongs'])
            songs=[s['id'] for s in all_songs() if d.get('fromFavorites') and s['id'] in fav]
            write_json(PLAYLISTS/(pid+'.json'),{'id':pid,'name':name,'ownerId':user['id'],'createdAt':datetime.now(timezone.utc).isoformat(),'songs':songs})
            return self.json_out({'playlist':{'id':pid,'name':name,'songsCount':len(songs)}},201)
        if path.startswith('/api/playlists/') and path.endswith('/songs'):
            user=require_user(self)
            if not user: return
            pid=path[len('/api/playlists/'):-len('/songs')].strip('/'); pf=safe(PLAYLISTS,pid+'.json')
            if not pf or not pf.is_file(): return self.json_out({'error':'Playlist not found'},404)
            pl=read_json(pf,{})
            if pl.get('ownerId')!=user['id']: return self.json_out({'error':'Нет доступа к плейлисту.'},403)
            sid=str(d.get('songId','')); valid={s['id'] for s in all_songs()}
            if sid not in valid: return self.json_out({'error':'Song not found'},404)
            ss=list(pl.get('songs',[]))
            if sid not in ss: ss.append(sid)
            pl['songs']=ss; write_json(pf,pl); return self.json_out({'songsCount':len(ss)})
        if path=='/api/developer/rescan':
            if not require_role(self,('admin','moderator')): return
            aa=artist_dirs(); return self.json_out({'message':'Library scan completed.','artists':len(aa),'albums':sum(len(album_dirs(a)) for a in aa),'songs':len(all_songs()),'widgets':len(widget_list()),'playlists':len(playlists_list())})
        if path=='/api/developer/restart':
            if not require_role(self,('admin',)): return
            self.json_out({'message':'Restart requested.'})
            def do_restart():
                time.sleep(.8); os.execv(sys.executable,[sys.executable,str(BASE/'app.py')])
            threading.Thread(target=do_restart,daemon=True).start(); return
        return self.json_out({'error':'Unknown endpoint'},404)
    def do_DELETE(self):
        path=unquote(urlparse(self.path).path)
        try: d=self.body()
        except Exception: d={}
        if path.startswith('/api/playlists/') and path.endswith('/songs'):
            user=require_user(self)
            if not user: return
            pid=path[len('/api/playlists/'):-len('/songs')].strip('/'); pf=safe(PLAYLISTS,pid+'.json')
            if not pf or not pf.is_file(): return self.json_out({'error':'Playlist not found'},404)
            pl=read_json(pf,{})
            if pl.get('ownerId')!=user['id']: return self.json_out({'error':'Нет доступа к плейлисту.'},403)
            pl['songs']=[x for x in pl.get('songs',[]) if x!=str(d.get('songId',''))]
            write_json(pf,pl); return self.json_out({'songsCount':len(pl['songs'])})
        if path.startswith('/api/playlists/'):
            user=require_user(self)
            if not user: return
            pid=path[len('/api/playlists/'):].strip('/'); pf=safe(PLAYLISTS,pid+'.json')
            if not pf or not pf.is_file(): return self.json_out({'error':'Playlist not found'},404)
            if read_json(pf,{}).get('ownerId')!=user['id']: return self.json_out({'error':'Нет доступа к плейлисту.'},403)
            pf.unlink(); return self.json_out({'deleted':True})
        return self.json_out({'error':'Unknown endpoint'},404)
    def log_message(self,fmt,*args): print('[InMusic] '+fmt%args)

if __name__=='__main__':
    if not LIBRARY.exists(): write_json(LIBRARY,{'favoriteArtists':[],'favoriteSongs':[]})
    print(f'InMusic: http://{HOST}:{PORT}/InMusic')
    if not users_data(): print('Первый зарегистрированный аккаунт получит роль администратора. Создай его до публикации сайта в интернете!')
    print(f'Project: {BASE}\nPress Ctrl+C to stop.')
    class Server(ThreadingHTTPServer): allow_reuse_address=True; daemon_threads=True
    try: Server((HOST,PORT),Handler).serve_forever()
    except KeyboardInterrupt: print('\nInMusic stopped.')
