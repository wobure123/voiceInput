import time

import pyperclip
from pynput.keyboard import Controller, Key


_kb = Controller()


class TextInjector:
    def inject(self, text: str, restore_clipboard: bool = False) -> bool:
        if not text:
            return False

        old_clip = None
        if restore_clipboard:
            try:
                old_clip = pyperclip.paste()
            except Exception:
                old_clip = None

        try:
            pyperclip.copy(text)
            time.sleep(0.05)
            _kb.press(Key.shift)
            _kb.press(Key.insert)
            _kb.release(Key.insert)
            _kb.release(Key.shift)
            time.sleep(0.1)

            if restore_clipboard and old_clip is not None:
                time.sleep(0.3)
                pyperclip.copy(old_clip)

            return True
        except Exception:
            return False

    @staticmethod
    def copy_only(text: str):
        if text:
            pyperclip.copy(text)
