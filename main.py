"""YTSub - Android app (Kivy). Receives YouTube links via Share, handles playlists."""
import os
import threading
import time
from pathlib import Path

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.text import LabelBase
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.modalview import ModalView
from kivy.uix.progressbar import ProgressBar
from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.switch import Switch
from kivy.uix.textinput import TextInput
from kivy.utils import platform

import core

try:  # Arabic shaping + right-to-left
    import arabic_reshaper
    from bidi.algorithm import get_display

    def ar(t):
        return get_display(arabic_reshaper.reshape(str(t)))
except Exception:  # pragma: no cover
    def ar(t):
        return str(t)

HERE = Path(__file__).resolve().parent
FONT = next((f for f in (HERE / "font.ttf", HERE / "assets" / "font.ttf") if f.exists()), None)
if FONT:
    LabelBase.register(name="Roboto", fn_regular=str(FONT))


def lbl(text, **kw):
    kw.setdefault("halign", "right")
    kw.setdefault("valign", "middle")
    w = Label(text=ar(text), **kw)
    w.bind(width=lambda inst, v: setattr(inst, "text_size", (v, None)))
    return w


def row(text, widget, h=48):
    r = BoxLayout(size_hint_y=None, height=dp(h), spacing=dp(8))
    r.add_widget(widget)
    r.add_widget(lbl(text))
    return r


class YTSubApp(App):
    title = "YTSub"

    # ------------------------------------------------------------ build
    def build(self):
        self.cfg_dir = Path(self.user_data_dir)
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_file = self.cfg_dir / "settings.json"
        self.hist_file = self.cfg_dir / "history.jsonl"
        self.s = core.Settings.load(self.cfg_file)
        if not self.s.out_dir:
            self.s.out_dir = self.default_out_dir()
        self.cancel = threading.Event()
        self.busy = False
        self._last_shared = None
        self.log_lines = []

        root = BoxLayout(orientation="vertical")

        bar = BoxLayout(size_hint_y=None, height=dp(52), padding=dp(6), spacing=dp(6))
        menu_btn = Button(text=ar("القائمة"), size_hint_x=None, width=dp(100))
        menu_btn.bind(on_release=lambda *_: self.menu.open())
        self.title_lbl = lbl("YTSub", font_size="18sp")
        bar.add_widget(menu_btn)
        bar.add_widget(self.title_lbl)
        root.add_widget(bar)

        self.sm = ScreenManager(transition=NoTransition())
        for sc in (self.build_home(), self.build_playlist(), self.build_history(),
                   self.build_settings(), self.build_about()):
            self.sm.add_widget(sc)
        root.add_widget(self.sm)

        # shared progress + log panel
        self.pb = ProgressBar(max=1, value=0, size_hint_y=None, height=dp(8))
        root.add_widget(self.pb)
        self.log_lbl = Label(size_hint_y=None, halign="right", valign="top", font_size="13sp")
        self.log_lbl.bind(width=lambda w, v: setattr(w, "text_size", (v, None)),
                          texture_size=lambda w, v: setattr(w, "height", v[1]))
        sv = ScrollView(size_hint_y=None, height=dp(130))
        sv.add_widget(self.log_lbl)
        root.add_widget(sv)
        self.cancel_btn = Button(text=ar("إلغاء"), size_hint_y=None, height=dp(44))
        self.cancel_btn.bind(on_release=lambda *_: self.cancel.set())
        root.add_widget(self.cancel_btn)

        self.build_menu()
        return root

    def default_out_dir(self):
        if platform == "android":
            try:
                from jnius import autoclass
                act = autoclass("org.kivy.android.PythonActivity").mActivity
                d = act.getExternalFilesDir(None)   # no storage permission needed
                if d:
                    return os.path.join(d.getAbsolutePath(), "YTSub")
            except Exception:
                pass
            return str(self.cfg_dir / "downloads")
        return str(Path.home() / "YTSub")

    # ------------------------------------------------------------ menu
    def build_menu(self):
        self.menu = ModalView(size_hint=(0.72, 1), pos_hint={"x": 0, "y": 0},
                              auto_dismiss=True, background_color=(0, 0, 0, 0.6))
        box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))
        box.add_widget(lbl("YTSub", font_size="22sp", size_hint_y=None, height=dp(56)))
        for text, name in (("الرئيسية (فيديو)", "home"), ("قائمة تشغيل", "playlist"),
                           ("السجل", "history"), ("الإعدادات", "settings"), ("حول التطبيق", "about")):
            b = Button(text=ar(text), size_hint_y=None, height=dp(52))
            b.bind(on_release=lambda _b, n=name: self.go(n))
            box.add_widget(b)
        box.add_widget(Label())
        self.menu.add_widget(box)

    def go(self, name):
        self.menu.dismiss()
        self.sm.current = name

    # ------------------------------------------------------------ option widgets
    def set_opt(self, attr, value):
        setattr(self.s, attr, value)
        try:
            self.s.save(self.cfg_file)
        except Exception as e:
            self.log(f"تعذّر حفظ الإعدادات: {e}")

    def switch_row(self, text, attr):
        sw = Switch(active=bool(getattr(self.s, attr)), size_hint_x=None, width=dp(90))
        sw.bind(active=lambda _i, v: self.set_opt(attr, v))
        return row(text, sw)

    def spinner_row(self, text, attr, values, cast=str):
        sp = Spinner(text=str(getattr(self.s, attr)), values=[str(v) for v in values],
                     size_hint_x=None, width=dp(120))
        sp.bind(text=lambda _i, v: self.set_opt(attr, cast(v)))
        return row(text, sp)

    # ------------------------------------------------------------ screens
    def build_home(self):
        sc = Screen(name="home")
        b = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        b.add_widget(lbl("رابط الفيديو (أو شاركه من يوتيوب مباشرة)", size_hint_y=None, height=dp(30)))
        r = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        self.url_in = TextInput(hint_text="https://youtu.be/...", multiline=False)
        paste = Button(text=ar("لصق"), size_hint_x=None, width=dp(80))
        paste.bind(on_release=lambda *_: setattr(
            self.url_in, "text", core.extract_url(Clipboard.paste() or "") or ""))
        r.add_widget(paste)
        r.add_widget(self.url_in)
        b.add_widget(r)
        b.add_widget(self.switch_row("تنزيل الترجمة", "get_subs"))
        b.add_widget(self.switch_row("ترجمتها للعربية", "translate"))
        b.add_widget(self.switch_row("ترجمة متوازية (أصلي + عربي)", "bilingual"))
        b.add_widget(self.switch_row("تنزيل الفيديو", "download_video"))
        go = Button(text=ar("ابدأ"), size_hint_y=None, height=dp(54))
        go.bind(on_release=lambda *_: self.start_single())
        b.add_widget(go)
        b.add_widget(Label())
        sc.add_widget(b)
        return sc

    def build_playlist(self):
        sc = Screen(name="playlist")
        b = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        b.add_widget(lbl("رابط قائمة التشغيل", size_hint_y=None, height=dp(30)))
        self.pl_url = TextInput(hint_text="https://youtube.com/playlist?list=...",
                                multiline=False, size_hint_y=None, height=dp(48))
        b.add_widget(self.pl_url)
        self.from_in = TextInput(hint_text="1", multiline=False, input_filter="int")
        self.to_in = TextInput(hint_text=ar("الأخير"), multiline=False, input_filter="int")
        rng = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        rng.add_widget(self.to_in)
        rng.add_widget(lbl("إلى"))
        rng.add_widget(self.from_in)
        rng.add_widget(lbl("من فيديو رقم"))
        b.add_widget(rng)
        b.add_widget(self.switch_row("تنزيل الترجمة", "get_subs"))
        b.add_widget(self.switch_row("ترجمتها للعربية", "translate"))
        b.add_widget(self.switch_row("تنزيل الفيديوهات", "download_video"))
        go = Button(text=ar("ابدأ تنزيل القائمة"), size_hint_y=None, height=dp(54))
        go.bind(on_release=lambda *_: self.start_playlist())
        b.add_widget(go)
        b.add_widget(Label())
        sc.add_widget(b)
        return sc

    def build_history(self):
        sc = Screen(name="history")
        b = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        self.hist_lbl = Label(size_hint_y=None, halign="right", valign="top", font_size="14sp")
        self.hist_lbl.bind(width=lambda w, v: setattr(w, "text_size", (v, None)),
                           texture_size=lambda w, v: setattr(w, "height", v[1]))
        sv = ScrollView()
        sv.add_widget(self.hist_lbl)
        b.add_widget(sv)
        clr = Button(text=ar("مسح السجل"), size_hint_y=None, height=dp(48))
        clr.bind(on_release=lambda *_: (core.clear_history(self.hist_file), self.refresh_history()))
        b.add_widget(clr)
        sc.add_widget(b)
        sc.bind(on_pre_enter=lambda *_: self.refresh_history())
        return sc

    def refresh_history(self):
        lines = []
        for rec in core.read_history(self.hist_file):
            lines.append(ar(f"{rec.get('time', '')}  {rec.get('label', '')}"))
            for f in rec.get("files", []):
                lines.append("   " + os.path.basename(f))
            for e in rec.get("errors", []):
                lines.append(ar("   خطأ: " + str(e)))
            lines.append("")
        self.hist_lbl.text = "\n".join(lines) or ar("لا يوجد سجل بعد")

    def build_settings(self):
        sc = Screen(name="settings")
        b = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        b.add_widget(self.spinner_row("جودة الفيديو", "quality", [360, 480, 720, 1080], int))
        b.add_widget(self.switch_row("صوت فقط", "audio_only"))
        b.add_widget(self.spinner_row("صيغة الترجمة", "sub_format", ["srt", "vtt", "txt"]))
        b.add_widget(self.spinner_row("لغة الترجمة", "target_lang",
                                      ["ar", "en", "fr", "es", "de", "tr", "ru"]))
        b.add_widget(self.spinner_row("عدد الطلبات المتوازية", "workers", [1, 2, 4, 6], int))
        b.add_widget(self.switch_row("بدء تلقائي عند المشاركة", "auto_start"))
        b.add_widget(self.switch_row("حذف [موسيقى] ونحوها", "strip_noise"))
        b.add_widget(lbl("مجلد الحفظ:", size_hint_y=None, height=dp(28)))
        d = Label(text=self.s.out_dir, size_hint_y=None, height=dp(48), font_size="12sp")
        d.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
        b.add_widget(d)
        b.add_widget(Label())
        sc.add_widget(b)
        return sc

    def build_about(self):
        sc = Screen(name="about")
        b = BoxLayout(orientation="vertical", padding=dp(16))
        b.add_widget(lbl("YTSub\nشارك رابط يوتيوب من أي تطبيق واختر YTSub.\n"
                         "ينزّل الترجمة ويترجمها للعربية، وينزّل الفيديو أو قائمة التشغيل."))
        sc.add_widget(b)
        return sc

    # ------------------------------------------------------------ log / progress
    def log(self, msg):
        Clock.schedule_once(lambda _dt: self._append(msg))

    def _append(self, msg):
        self.log_lines.append(str(msg))
        self.log_lines = self.log_lines[-60:]
        self.log_lbl.text = "\n".join(ar(x) for x in self.log_lines)

    def set_progress(self, v):
        v = max(0.0, min(1.0, float(v)))
        Clock.schedule_once(lambda _dt: setattr(self.pb, "value", v))

    # ------------------------------------------------------------ jobs
    def run_job(self, fn, label):
        if self.busy:
            self.log("هناك عملية جارية الآن")
            return
        if not (self.s.get_subs or self.s.download_video):
            self.log("فعّل تنزيل الترجمة أو الفيديو أولًا")
            return
        self.busy = True
        self.cancel.clear()
        self.set_progress(0)

        def work():
            res = {}
            try:
                res = fn(self.log, self.set_progress, self.cancel)
                for e in res.get("errors", []):
                    self.log(f"تنبيه: {e}")
                self.log("اكتملت العملية")
                self.set_progress(1)
            except core.Cancelled:
                self.log("تم الإلغاء")
            except Exception as e:
                self.log(f"خطأ: {e}")
                res = {"errors": [str(e)]}
            finally:
                try:
                    core.add_history(self.hist_file, {
                        "time": time.strftime("%Y-%m-%d %H:%M"), "label": label,
                        "files": res.get("files", []), "errors": res.get("errors", [])})
                except Exception:
                    pass
                self.busy = False

        threading.Thread(target=work, daemon=True).start()

    def start_single(self):
        url = self.url_in.text.strip()
        if not core.get_video_id(url):
            self.log("رابط الفيديو غير صحيح")
            return
        self.run_job(lambda lg, pr, c: core.process_video(url, self.s, self.s.out_dir, lg, pr, c),
                     url)

    def start_playlist(self):
        url = self.pl_url.text.strip()
        if not core.get_playlist_id(url):
            self.log("رابط القائمة غير صحيح")
            return
        start = int(self.from_in.text or 1)
        end = int(self.to_in.text) if self.to_in.text else None
        self.run_job(lambda lg, pr, c: core.run_playlist(
            url, self.s, self.s.out_dir, start, end, lg, pr, c), "playlist: " + url)

    # ------------------------------------------------------------ Android share intent
    def on_start(self):
        if platform == "android":
            try:
                from android import activity
                activity.bind(on_new_intent=self.check_intent)
            except Exception as e:
                self.log(f"intent: {e}")
            self.check_intent()

    def on_pause(self):
        return True

    def on_resume(self):
        self.check_intent()
        return True

    def check_intent(self, intent=None):
        if platform != "android":
            return
        try:
            from jnius import autoclass
            activity_cls = autoclass("org.kivy.android.PythonActivity")
            intent = intent or activity_cls.mActivity.getIntent()
            if intent is None or intent.getAction() != "android.intent.action.SEND":
                return
            text = intent.getStringExtra("android.intent.extra.TEXT")
        except Exception as e:
            self.log(f"intent: {e}")
            return
        if text and text != self._last_shared:
            self._last_shared = text
            Clock.schedule_once(lambda _dt: self.handle_shared(text))

    def handle_shared(self, text):
        url = core.extract_url(text) or text
        if core.get_video_id(url):
            self.url_in.text = url
            self.sm.current = "home"
            self.log("تم استلام رابط فيديو")
            if self.s.auto_start:
                self.start_single()
        elif core.get_playlist_id(url):
            self.pl_url.text = url
            self.sm.current = "playlist"
            self.log("تم استلام قائمة تشغيل، اضغط ابدأ")
        else:
            self.log("المشاركة لا تحتوي على رابط يوتيوب")


if __name__ == "__main__":
    YTSubApp().run()
