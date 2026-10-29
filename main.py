"""Manhwa Reader — Discord-бот для чтения манхвы (всё в одном файле).

Запуск:
    pip install discord.py aiosqlite Pillow python-dotenv
    # в .env: DISCORD_TOKEN=...  (и желательно DEV_GUILD_ID=...)
    python bot.py
"""
import asyncio
import io
import logging
import os
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

import aiosqlite
import discord
from discord import app_commands
from dotenv import load_dotenv
from PIL import Image

# =====================================================================
# CONFIG
# =====================================================================
load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
LIB_DIR = DATA_DIR / "library"   # готовые страницы: library/<title_id>/<chapter_id>/0001.jpg
TMP_DIR = DATA_DIR / "tmp"       # временные файлы при загрузке

# Пользователи, которым разрешено управлять библиотекой (помимо тех, у кого есть право «Управление сервером»)
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

# Для разработки: ID сервера, на котором слэш-команды появятся мгновенно
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None

# Высота одной страницы = ширина * SLICE_RATIO. Длинные полосы режутся на части этой высоты.
SLICE_RATIO = float(os.getenv("SLICE_RATIO", "1.6"))
MAX_WIDTH = int(os.getenv("MAX_WIDTH", "1000"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("manhwa")


# =====================================================================
# IMAGING: чтение zip/cbz, нарезка длинных полос, сохранение в JPEG
# =====================================================================
Image.MAX_IMAGE_PIXELS = 200_000_000

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
MAX_IMAGES = 500
MAX_UNPACKED = 700 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024  # запас до лимита загрузки Discord


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def iter_zip(data: bytes) -> Iterator[bytes]:
    """Отдаёт байты изображений из zip/cbz в «естественном» порядке имён."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    infos = [
        i for i in zf.infolist()
        if not i.is_dir()
        and Path(i.filename).suffix.lower() in IMG_EXT
        and "__MACOSX" not in i.filename
        and not Path(i.filename).name.startswith(".")
    ]
    infos.sort(key=lambda i: natural_key(i.filename))
    if not infos:
        raise ValueError("В архиве нет изображений.")
    if len(infos) > MAX_IMAGES:
        raise ValueError(f"Слишком много файлов (максимум {MAX_IMAGES}).")
    if sum(i.file_size for i in infos) > MAX_UNPACKED:
        raise ValueError("Архив слишком большой после распаковки.")
    for info in infos:
        yield zf.read(info)


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[3])
        return bg
    return img.convert("RGB")


def _find_cut(gray: Image.Image, target: int, window: int) -> int:
    """Ищет ближайшую к target однотонную строку (пустое поле между панелями), чтобы не резать по картинке."""
    w, h = gray.size
    for off in range(window + 1):
        for y in (target - off, target + off):
            if 0 <= y < h:
                lo, hi = gray.crop((0, y, w, y + 1)).getextrema()
                if hi - lo <= 10:
                    return y
    return target


def _slices(img: Image.Image) -> Iterator[Image.Image]:
    w, h = img.size
    page_h = max(int(w * SLICE_RATIO), 400)
    if h <= page_h * 1.3:
        yield img
        return
    gray = img.convert("L")
    y = 0
    while h - y > page_h * 1.3:
        cut = _find_cut(gray, y + page_h, int(page_h * 0.2))
        yield img.crop((0, y, w, cut))
        y = cut
    yield img.crop((0, y, w, h))


def _save_jpeg(img: Image.Image, path: Path):
    for quality in (88, 75, 60, 45):
        img.save(path, "JPEG", quality=quality, optimize=True)
        if path.stat().st_size <= MAX_FILE_BYTES:
            return


def process_images(images: Iterable[bytes], dest: Path) -> int:
    """Конвертирует и нарезает изображения, пишет в dest/0001.jpg, ... Возвращает число страниц."""
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        for idx, raw in enumerate(images, 1):
            try:
                img = Image.open(io.BytesIO(raw))
                img.load()
            except Exception as e:
                raise ValueError(f"Файл №{idx} не похож на изображение.") from e
            img = _to_rgb(img)
            if img.width > MAX_WIDTH:
                img = img.resize(
                    (MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)), Image.LANCZOS
                )
            for part in _slices(img):
                n += 1
                _save_jpeg(part, dest / f"{n:04d}.jpg")
        if n == 0:
            raise ValueError("Не найдено ни одного изображения.")
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return n


def save_cover(raw: bytes, path: Path):
    img = _to_rgb(Image.open(io.BytesIO(raw)))
    img.thumbnail((600, 900))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=90)


# =====================================================================
# DB
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS titles(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    genres      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'ongoing',
    has_cover   INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS chapters(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id   INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    number     REAL NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    pages      INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(title_id, number)
);
CREATE TABLE IF NOT EXISTS progress(
    user_id    INTEGER NOT NULL,
    title_id   INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    page       INTEGER NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY(user_id, title_id)
);
"""


def _like(value: str) -> str:
    esc = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def all(self, sql, args=()):
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchall()

    async def one(self, sql, args=()):
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def run(self, sql, args=()):
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur.lastrowid

    # ---------- titles ----------
    async def add_title(self, name, description, genres, status, has_cover) -> int:
        return await self.run(
            "INSERT INTO titles(name, description, genres, status, has_cover) VALUES(?,?,?,?,?)",
            (name, description, genres, status, int(has_cover)),
        )

    async def title(self, title_id: int):
        return await self.one("SELECT * FROM titles WHERE id=?", (title_id,))

    async def find_title(self, value: str):
        value = (value or "").strip()
        if not value:
            return None
        if value.isdigit():
            row = await self.title(int(value))
            if row:
                return row
        row = await self.one("SELECT * FROM titles WHERE name=?", (value,))
        if row:
            return row
        return await self.one(
            "SELECT * FROM titles WHERE name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 1", (_like(value),)
        )

    async def search_titles(self, query: str, limit: int = 25):
        return await self.all(
            "SELECT id, name FROM titles WHERE name LIKE ? ESCAPE '\\' ORDER BY name LIMIT ?",
            (_like(query), limit),
        )

    async def titles_with_counts(self):
        return await self.all(
            "SELECT t.*, (SELECT COUNT(*) FROM chapters c WHERE c.title_id=t.id) AS n "
            "FROM titles t ORDER BY t.name COLLATE NOCASE"
        )

    async def title_with_count(self, title_id: int):
        return await self.one(
            "SELECT t.*, (SELECT COUNT(*) FROM chapters c WHERE c.title_id=t.id) AS n "
            "FROM titles t WHERE t.id=?",
            (title_id,),
        )

    async def update_title(self, title_id: int, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.run(f"UPDATE titles SET {cols} WHERE id=?", (*fields.values(), title_id))

    async def delete_title(self, title_id: int):
        await self.run("DELETE FROM titles WHERE id=?", (title_id,))

    # ---------- chapters ----------
    async def chapters(self, title_id: int):
        return await self.all("SELECT * FROM chapters WHERE title_id=? ORDER BY number", (title_id,))

    async def chapter_by_number(self, title_id: int, number: float):
        return await self.one("SELECT * FROM chapters WHERE title_id=? AND number=?", (title_id, number))

    async def add_chapter(self, title_id, number, name, pages) -> int:
        return await self.run(
            "INSERT INTO chapters(title_id, number, name, pages) VALUES(?,?,?,?)",
            (title_id, number, name, pages),
        )

    async def delete_chapter(self, chapter_id: int):
        await self.run("DELETE FROM chapters WHERE id=?", (chapter_id,))

    # ---------- progress ----------
    async def progress(self, user_id: int, title_id: int):
        return await self.one("SELECT * FROM progress WHERE user_id=? AND title_id=?", (user_id, title_id))

    async def set_progress(self, user_id, title_id, chapter_id, page):
        await self.run(
            "INSERT INTO progress(user_id, title_id, chapter_id, page) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, title_id) DO UPDATE SET "
            "chapter_id=excluded.chapter_id, page=excluded.page, updated_at=strftime('%s','now')",
            (user_id, title_id, chapter_id, page),
        )


# =====================================================================
# UI: читалка, библиотека, карточки тайтлов, кнопки «Читать» / «Поделиться»
# =====================================================================
COLOR = discord.Color.from_str("#7c5cff")
STATUS = {"ongoing": "Выходит", "completed": "Завершён", "hiatus": "Заморожен"}


def fmt(n) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def page_path(title_id, chapter_id, page):
    return LIB_DIR / str(title_id) / str(chapter_id) / f"{page:04d}.jpg"


def cover_path(title_id):
    return LIB_DIR / str(title_id) / "cover.jpg"


def chapter_label(ch) -> str:
    return f"Глава {fmt(ch['number'])}" + (f" — {ch['name']}" if ch["name"] else "")


def title_card(t, n_chapters: int, chapter=None):
    e = discord.Embed(title=t["name"], description=t["description"] or None, color=COLOR)
    if chapter is not None:
        e.add_field(name="Читать с", value=chapter_label(chapter), inline=False)
    if t["genres"]:
        e.add_field(name="Жанры", value=t["genres"])
    e.add_field(name="Статус", value=STATUS.get(t["status"], t["status"]))
    e.add_field(name="Глав", value=str(n_chapters))
    file = None
    if t["has_cover"] and cover_path(t["id"]).exists():
        file = discord.File(cover_path(t["id"]), filename="cover.jpg")
        e.set_image(url="attachment://cover.jpg")
    return e, file


async def send_share(interaction: discord.Interaction, t, n_chapters: int, chapter=None):
    """Публикует в канале карточку тайтла (или главы) с кнопкой «Читать»."""
    e, f = title_card(t, n_chapters, chapter)
    view = discord.ui.View(timeout=None)
    view.add_item(ReadButton(t["id"], chapter["id"] if chapter else None))
    what = f"главой **{fmt(chapter['number'])}**" if chapter else "тайтлом"
    kwargs = {"file": f} if f else {}
    await interaction.response.send_message(
        content=f"🔗 {interaction.user.mention} делится {what}:",
        embed=e,
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
        **kwargs,
    )


# ---------------------------------------------------------------- persistent buttons
class ReadButton(discord.ui.DynamicItem[discord.ui.Button], template=r"read:(?P<tid>\d+)(?::(?P<cid>\d+))?"):
    """Кнопка «Читать» — работает даже после перезапуска бота."""

    def __init__(self, title_id: int, chapter_id: int | None = None):
        self.tid, self.cid = title_id, chapter_id
        cid = f"read:{title_id}" + (f":{chapter_id}" if chapter_id else "")
        super().__init__(discord.ui.Button(label="Читать", emoji="📖", style=discord.ButtonStyle.primary, custom_id=cid))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["tid"]), int(match["cid"]) if match["cid"] else None)

    async def callback(self, interaction: discord.Interaction):
        await open_reader(interaction, self.tid, self.cid)


class ShareButton(discord.ui.DynamicItem[discord.ui.Button], template=r"share:(?P<tid>\d+)"):
    def __init__(self, title_id: int):
        self.tid = title_id
        super().__init__(
            discord.ui.Button(label="Поделиться", emoji="🔗", style=discord.ButtonStyle.secondary, custom_id=f"share:{title_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["tid"]))

    async def callback(self, interaction: discord.Interaction):
        t = await interaction.client.db.title_with_count(self.tid)
        if not t:
            return await interaction.response.send_message("Тайтл удалён.", ephemeral=True)
        await send_share(interaction, t, t["n"])


# ---------------------------------------------------------------- reader
async def open_reader(interaction: discord.Interaction, title_id: int, chapter_id: int | None = None):
    db = interaction.client.db
    t = await db.title(title_id)
    chapters = await db.chapters(title_id) if t else []
    if not t or not chapters:
        return await interaction.response.send_message("В этом тайтле пока нет глав.", ephemeral=True)

    ci, page = 0, 1
    if chapter_id is not None:
        ci = next((i for i, c in enumerate(chapters) if c["id"] == chapter_id), 0)
    else:
        p = await db.progress(interaction.user.id, title_id)
        if p:
            ci = next((i for i, c in enumerate(chapters) if c["id"] == p["chapter_id"]), 0)
            page = min(p["page"], chapters[ci]["pages"])

    view = Reader(db, interaction.user.id, t, chapters, ci, page)
    await db.set_progress(interaction.user.id, title_id, view.ch["id"], view.page)
    embed, file = view.render()
    await interaction.response.send_message(embed=embed, file=file, view=view, ephemeral=True)
    view.origin = interaction


class JumpModal(discord.ui.Modal, title="Перейти к странице"):
    num = discord.ui.TextInput(label="Номер страницы", max_length=4)

    def __init__(self, reader: "Reader"):
        super().__init__()
        self.reader = reader
        self.num.placeholder = f"1–{reader.ch['pages']}"

    async def on_submit(self, interaction: discord.Interaction):
        try:
            p = int(self.num.value)
        except ValueError:
            return await interaction.response.send_message("Нужно число.", ephemeral=True)
        self.reader.page = min(max(p, 1), self.reader.ch["pages"])
        await self.reader.show(interaction)


class Reader(discord.ui.View):
    def __init__(self, db, user_id, title, chapters, ci, page):
        super().__init__(timeout=840)
        self.db, self.user_id, self.t, self.chapters = db, user_id, title, chapters
        self.ci, self.page = ci, page
        self.origin: discord.Interaction | None = None
        self._sync()

    @property
    def ch(self):
        return self.chapters[self.ci]

    def _sync(self):
        last_ch = len(self.chapters) - 1
        self.prev_ch.disabled = self.ci == 0
        self.next_ch.disabled = self.ci == last_ch
        self.prev_page.disabled = self.ci == 0 and self.page <= 1
        self.next_page.disabled = self.ci == last_ch and self.page >= self.ch["pages"]
        self.jump.label = f"{self.page}/{self.ch['pages']}"

        n = len(self.chapters)
        start = max(0, min(self.ci - 12, n - 25))
        self.chapter_select.options = [
            discord.SelectOption(label=chapter_label(self.chapters[i])[:100], value=str(i), default=(i == self.ci))
            for i in range(start, min(n, start + 25))
        ]

    def render(self):
        ch = self.ch
        e = discord.Embed(title=self.t["name"], description=chapter_label(ch), color=COLOR)
        e.set_image(url="attachment://page.jpg")
        e.set_footer(text=f"Стр. {self.page}/{ch['pages']}")
        f = discord.File(page_path(self.t["id"], ch["id"], self.page), filename="page.jpg")
        return e, f

    async def show(self, interaction: discord.Interaction):
        self._sync()
        await self.db.set_progress(self.user_id, self.t["id"], self.ch["id"], self.page)
        e, f = self.render()
        await interaction.response.edit_message(embed=e, attachments=[f], view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это чужая читалка — откройте свою командой /read.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except Exception:
            pass

    # --- row 0
    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary, row=0)
    async def prev_ch(self, interaction, button):
        self.ci, self.page = self.ci - 1, 1
        await self.show(interaction)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.primary, row=0)
    async def prev_page(self, interaction, button):
        if self.page > 1:
            self.page -= 1
        elif self.ci > 0:
            self.ci -= 1
            self.page = self.ch["pages"]
        await self.show(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, row=0)
    async def jump(self, interaction, button):
        await interaction.response.send_modal(JumpModal(self))

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.primary, row=0)
    async def next_page(self, interaction, button):
        if self.page < self.ch["pages"]:
            self.page += 1
        elif self.ci < len(self.chapters) - 1:
            self.ci += 1
            self.page = 1
        await self.show(interaction)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, row=0)
    async def next_ch(self, interaction, button):
        self.ci, self.page = self.ci + 1, 1
        await self.show(interaction)

    # --- row 1
    @discord.ui.select(placeholder="Выбрать главу", row=1, options=[discord.SelectOption(label="…")])
    async def chapter_select(self, interaction, select):
        self.ci, self.page = int(select.values[0]), 1
        await self.show(interaction)

    # --- row 2
    @discord.ui.button(label="Поделиться главой", emoji="🔗", style=discord.ButtonStyle.secondary, row=2)
    async def share(self, interaction, button):
        await send_share(interaction, self.t, len(self.chapters), self.ch)

    @discord.ui.button(label="Закрыть", style=discord.ButtonStyle.danger, row=2)
    async def close(self, interaction, button):
        await interaction.response.edit_message(content="Чтение закрыто. Прогресс сохранён.", embed=None, attachments=[], view=None)
        self.stop()


# ---------------------------------------------------------------- library
class LibraryView(discord.ui.View):
    PER = 10

    def __init__(self, titles):
        super().__init__(timeout=600)
        self.titles, self.page = titles, 0
        self._build()

    @property
    def total_pages(self):
        return max(1, -(-len(self.titles) // self.PER))

    def _chunk(self):
        s = self.page * self.PER
        return self.titles[s : s + self.PER]

    def embed(self):
        lines = [f"**{t['name']}** — {t['n']} гл. · {STATUS.get(t['status'], t['status'])}" for t in self._chunk()]
        e = discord.Embed(title="📚 Библиотека", description="\n".join(lines) or "Пока пусто.", color=COLOR)
        e.set_footer(text=f"Стр. {self.page + 1}/{self.total_pages} · тайтлов: {len(self.titles)}")
        return e

    def _build(self):
        self.clear_items()
        chunk = self._chunk()
        if chunk:
            sel = discord.ui.Select(
                placeholder="Открыть тайтл…",
                options=[
                    discord.SelectOption(
                        label=t["name"][:100], value=str(t["id"]), description=f"{t['n']} гл. · {STATUS.get(t['status'], '')}"
                    )
                    for t in chunk
                ],
            )
            sel.callback = self._pick
            self.add_item(sel)
        prev = discord.ui.Button(emoji="◀", disabled=self.page == 0)
        nxt = discord.ui.Button(emoji="▶", disabled=self.page >= self.total_pages - 1)
        prev.callback, nxt.callback = self._prev, self._next
        self.add_item(prev)
        self.add_item(nxt)

    async def _prev(self, interaction):
        self.page -= 1
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _next(self, interaction):
        self.page += 1
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _pick(self, interaction):
        tid = int(interaction.data["values"][0])
        t = next((x for x in self.titles if x["id"] == tid), None)
        if not t:
            return await interaction.response.send_message("Тайтл не найден.", ephemeral=True)
        e, f = title_card(t, t["n"])
        v = discord.ui.View(timeout=300)
        v.add_item(ReadButton(tid))
        v.add_item(ShareButton(tid))
        kwargs = {"file": f} if f else {}
        await interaction.response.send_message(embed=e, view=v, ephemeral=True, **kwargs)


# =====================================================================
# BOT
# =====================================================================
@dataclass
class UploadSession:
    title_id: int
    number: float
    name: str
    channel_id: int
    dir: Path
    files: list = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ManhwaBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # нужно, чтобы видеть вложения при пакетной загрузке глав
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = DB(DATA_DIR / "manhwa.db")
        self.sessions: dict[int, UploadSession] = {}

    async def setup_hook(self):
        LIB_DIR.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        await self.db.connect()
        self.add_dynamic_items(ReadButton, ShareButton)
        if DEV_GUILD_ID:
            guild = discord.Object(DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Вошёл как %s", self.user)

    async def on_message(self, msg: discord.Message):
        """Собирает картинки для активной сессии /chapter_begin."""
        s = self.sessions.get(msg.author.id)
        if not s or msg.channel.id != s.channel_id or not msg.attachments:
            return
        saved = 0
        async with s.lock:
            for i, a in enumerate(msg.attachments):
                ext = Path(a.filename).suffix.lower()
                if ext not in IMG_EXT:
                    continue
                p = s.dir / f"{msg.id}_{i:02d}{ext}"
                await a.save(p)
                s.files.append(p)
                saved += 1
        if saved:
            try:
                await msg.add_reaction("✅")
            except discord.HTTPException:
                pass


bot = ManhwaBot()
tree = bot.tree


# ------------------------------------------------------------------ helpers
def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id in ADMIN_IDS:
            return True
        if interaction.guild and interaction.user.guild_permissions.manage_guild:
            return True
        raise app_commands.CheckFailure("Эта команда доступна только администраторам библиотеки.")

    return app_commands.check(predicate)


async def say(interaction: discord.Interaction, text: str, **kw):
    kw.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(text, **kw)
    else:
        await interaction.response.send_message(text, **kw)


async def get_title(interaction: discord.Interaction, value: str):
    t = await bot.db.find_title(value)
    if not t:
        await say(interaction, "Тайтл не найден. Выберите его из подсказок.")
    return t


async def title_autocomplete(interaction: discord.Interaction, current: str):
    rows = await bot.db.search_titles(current)
    return [app_commands.Choice(name=r["name"][:100], value=str(r["id"])) for r in rows]


async def store_chapter(title_id: int, number: float, name: str, images: Callable[[], Iterable[bytes]], replace: bool):
    """Обрабатывает картинки, кладёт страницы на диск и записывает главу в БД."""
    existing = await bot.db.chapter_by_number(title_id, number)
    if existing and not replace:
        raise ValueError(f"Глава {fmt(number)} уже есть. Укажите replace=True, чтобы заменить её.")

    tmp = TMP_DIR / uuid.uuid4().hex
    pages = await asyncio.to_thread(lambda: process_images(images(), tmp))

    if existing:
        await bot.db.delete_chapter(existing["id"])
        shutil.rmtree(LIB_DIR / str(title_id) / str(existing["id"]), ignore_errors=True)
    cid = await bot.db.add_chapter(title_id, number, name, pages)
    dest = LIB_DIR / str(title_id) / str(cid)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), str(dest))
    return pages


STATUS_CHOICES = [
    app_commands.Choice(name="Выходит", value="ongoing"),
    app_commands.Choice(name="Завершён", value="completed"),
    app_commands.Choice(name="Заморожен", value="hiatus"),
]


# ------------------------------------------------------------------ команды для всех
@tree.command(name="library", description="Библиотека: все тайтлы")
async def library(interaction: discord.Interaction):
    titles = await bot.db.titles_with_counts()
    view = LibraryView(titles)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


@tree.command(name="read", description="Читать тайтл (с того места, где остановились)")
@app_commands.describe(title="Название тайтла", chapter="Номер главы (по умолчанию — продолжить)")
@app_commands.autocomplete(title=title_autocomplete)
async def read(interaction: discord.Interaction, title: str, chapter: Optional[float] = None):
    t = await get_title(interaction, title)
    if not t:
        return
    chapter_id = None
    if chapter is not None:
        ch = await bot.db.chapter_by_number(t["id"], chapter)
        if not ch:
            return await say(interaction, f"Главы {fmt(chapter)} нет в «{t['name']}».")
        chapter_id = ch["id"]
    await open_reader(interaction, t["id"], chapter_id)


@tree.command(name="share", description="Поделиться тайтлом в этом канале")
@app_commands.describe(title="Название тайтла", chapter="Номер главы (необязательно)")
@app_commands.autocomplete(title=title_autocomplete)
async def share(interaction: discord.Interaction, title: str, chapter: Optional[float] = None):
    t = await get_title(interaction, title)
    if not t:
        return
    full = await bot.db.title_with_count(t["id"])
    ch = None
    if chapter is not None:
        ch = await bot.db.chapter_by_number(t["id"], chapter)
        if not ch:
            return await say(interaction, f"Главы {fmt(chapter)} нет в «{t['name']}».")
    await send_share(interaction, t, full["n"], ch)


# ------------------------------------------------------------------ админ: тайтлы
@tree.command(name="title_add", description="[админ] Добавить тайтл")
@is_admin()
@app_commands.describe(name="Название", description="Описание", genres="Жанры через запятую", status="Статус", cover="Обложка")
@app_commands.choices(status=STATUS_CHOICES)
async def title_add(
    interaction: discord.Interaction,
    name: str,
    description: str = "",
    genres: str = "",
    status: Optional[app_commands.Choice[str]] = None,
    cover: Optional[discord.Attachment] = None,
):
    await interaction.response.defer(ephemeral=True)
    try:
        tid = await bot.db.add_title(name.strip(), description, genres, status.value if status else "ongoing", bool(cover))
    except aiosqlite.IntegrityError:
        return await say(interaction, "Тайтл с таким названием уже существует.")
    if cover:
        try:
            await asyncio.to_thread(save_cover, await cover.read(), cover_path(tid))
        except Exception:
            await bot.db.update_title(tid, has_cover=0)
            await say(interaction, "Тайтл создан, но обложку прочитать не удалось.")
            return
    await say(interaction, f"✅ Тайтл «{name}» создан. Теперь добавьте главы: `/chapter_add` или `/chapter_begin`.")


@tree.command(name="title_edit", description="[админ] Изменить тайтл")
@is_admin()
@app_commands.autocomplete(title=title_autocomplete)
@app_commands.choices(status=STATUS_CHOICES)
async def title_edit(
    interaction: discord.Interaction,
    title: str,
    new_name: Optional[str] = None,
    description: Optional[str] = None,
    genres: Optional[str] = None,
    status: Optional[app_commands.Choice[str]] = None,
    cover: Optional[discord.Attachment] = None,
):
    t = await get_title(interaction, title)
    if not t:
        return
    await interaction.response.defer(ephemeral=True)
    fields = {}
    if new_name:
        fields["name"] = new_name.strip()
    if description is not None:
        fields["description"] = description
    if genres is not None:
        fields["genres"] = genres
    if status:
        fields["status"] = status.value
    if cover:
        await asyncio.to_thread(save_cover, await cover.read(), cover_path(t["id"]))
        fields["has_cover"] = 1
    try:
        await bot.db.update_title(t["id"], **fields)
    except aiosqlite.IntegrityError:
        return await say(interaction, "Тайтл с таким названием уже существует.")
    await say(interaction, "✅ Обновлено.")


@tree.command(name="title_delete", description="[админ] Удалить тайтл со всеми главами")
@is_admin()
@app_commands.describe(confirm="Поставьте True для подтверждения")
@app_commands.autocomplete(title=title_autocomplete)
async def title_delete(interaction: discord.Interaction, title: str, confirm: bool = False):
    t = await get_title(interaction, title)
    if not t:
        return
    if not confirm:
        return await say(interaction, "Это удалит тайтл и все его главы. Повторите команду с confirm=True.")
    await bot.db.delete_title(t["id"])
    shutil.rmtree(LIB_DIR / str(t["id"]), ignore_errors=True)
    await say(interaction, f"🗑 Тайтл «{t['name']}» удалён.")


# ------------------------------------------------------------------ админ: главы
@tree.command(name="chapter_add", description="[админ] Добавить главу из ZIP/CBZ архива с картинками")
@is_admin()
@app_commands.describe(number="Номер главы (можно 12.5)", name="Название главы", file="ZIP или CBZ", replace="Заменить, если глава уже есть")
@app_commands.autocomplete(title=title_autocomplete)
async def chapter_add(
    interaction: discord.Interaction,
    title: str,
    number: float,
    file: discord.Attachment,
    name: str = "",
    replace: bool = False,
):
    t = await get_title(interaction, title)
    if not t:
        return
    if Path(file.filename).suffix.lower() not in (".zip", ".cbz"):
        return await say(interaction, "Нужен архив .zip или .cbz с картинками внутри.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        data = await file.read()
        pages = await store_chapter(t["id"], number, name, lambda: iter_zip(data), replace)
    except (ValueError, zipfile.BadZipFile) as e:
        return await say(interaction, f"❌ {e}")
    except Exception:
        log.exception("chapter_add failed")
        return await say(interaction, "❌ Не удалось обработать архив.")
    await say(interaction, f"✅ «{t['name']}», глава {fmt(number)}: {pages} стр.")


@tree.command(name="chapter_begin", description="[админ] Начать загрузку главы пачками картинок в этом канале")
@is_admin()
@app_commands.describe(number="Номер главы", name="Название главы")
@app_commands.autocomplete(title=title_autocomplete)
async def chapter_begin(interaction: discord.Interaction, title: str, number: float, name: str = ""):
    t = await get_title(interaction, title)
    if not t:
        return
    if interaction.user.id in bot.sessions:
        return await say(interaction, "У вас уже есть активная загрузка. Завершите её `/chapter_end` или отмените `/chapter_cancel`.")
    d = TMP_DIR / f"session_{uuid.uuid4().hex}"
    d.mkdir(parents=True)
    bot.sessions[interaction.user.id] = UploadSession(t["id"], number, name, interaction.channel_id, d)
    await say(
        interaction,
        f"📥 Загрузка главы {fmt(number)} «{t['name']}» начата.\n"
        "Отправляйте в этот канал сообщения с картинками (до 10 вложений в сообщении) **по порядку**. "
        "Бот поставит ✅ на каждое принятое сообщение. Когда всё отправлено — `/chapter_end`.",
    )


@tree.command(name="chapter_end", description="[админ] Завершить загрузку главы и опубликовать")
@is_admin()
@app_commands.describe(replace="Заменить, если глава уже есть")
async def chapter_end(interaction: discord.Interaction, replace: bool = False):
    s = bot.sessions.get(interaction.user.id)
    if not s:
        return await say(interaction, "Нет активной загрузки. Начните с `/chapter_begin`.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with s.lock:
        files = sorted(s.files, key=lambda p: p.name)
    if not files:
        return await say(interaction, "Картинок ещё нет. Отправьте их в канал или отмените `/chapter_cancel`.")
    try:
        pages = await store_chapter(s.title_id, s.number, s.name, lambda: (p.read_bytes() for p in files), replace)
    except ValueError as e:
        return await say(interaction, f"❌ {e}")
    except Exception:
        log.exception("chapter_end failed")
        return await say(interaction, "❌ Не удалось обработать изображения.")
    bot.sessions.pop(interaction.user.id, None)
    shutil.rmtree(s.dir, ignore_errors=True)
    await say(interaction, f"✅ Глава {fmt(s.number)} опубликована: {pages} стр.")


@tree.command(name="chapter_cancel", description="[админ] Отменить текущую загрузку главы")
@is_admin()
async def chapter_cancel(interaction: discord.Interaction):
    s = bot.sessions.pop(interaction.user.id, None)
    if not s:
        return await say(interaction, "Нет активной загрузки.")
    shutil.rmtree(s.dir, ignore_errors=True)
    await say(interaction, "Загрузка отменена.")


@tree.command(name="chapter_delete", description="[админ] Удалить главу")
@is_admin()
@app_commands.autocomplete(title=title_autocomplete)
async def chapter_delete(interaction: discord.Interaction, title: str, number: float):
    t = await get_title(interaction, title)
    if not t:
        return
    ch = await bot.db.chapter_by_number(t["id"], number)
    if not ch:
        return await say(interaction, "Такой главы нет.")
    await bot.db.delete_chapter(ch["id"])
    shutil.rmtree(LIB_DIR / str(t["id"]) / str(ch["id"]), ignore_errors=True)
    await say(interaction, f"🗑 Глава {fmt(number)} удалена.")


# ------------------------------------------------------------------ ошибки
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return await say(interaction, f"⛔ {error}")
    log.exception("Ошибка команды", exc_info=error)
    await say(interaction, "Что-то пошло не так. Попробуйте ещё раз.")


if __name__ == "__main__":
    bot.run(TOKEN)
