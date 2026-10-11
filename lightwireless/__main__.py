"""Entrada: python -m lightwireless [--bandeja] | LightWireless.exe [--bandeja | --autoteste]"""
from __future__ import annotations

import logging
import logging.handlers
import sys

from . import __version__, estilo
from .caminhos import relatorios


def _log():
    h = logging.handlers.RotatingFileHandler(relatorios() / "lightwireless.log", maxBytes=1_000_000,
                                             backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    raiz.addHandler(h)
    logging.getLogger("tinytuya").setLevel(logging.WARNING)
    # o SDK da Tuya despeja as respostas (com a chave) em DEBUG: nunca abaixo de WARNING
    logging.getLogger("tuya_sharing").setLevel(logging.WARNING)


def _unico() -> bool:
    """Uma instancia so. A segunda pede a primeira para aparecer e sai."""
    try:
        import ctypes
        _unico.m = ctypes.windll.kernel32.CreateMutexW(None, False, "LightWireless_unico")
        if ctypes.windll.kernel32.GetLastError() == 183:
            if "--bandeja" not in sys.argv:          # quem sobe pelo Windows sai quieto
                (relatorios() / ".mostrar").write_text("1")
            return False
    except Exception:
        pass
    return True


def _autoteste():
    """--autoteste: confere o empacotado (.exe) sem janela nem lampada de verdade:
    carrega o que o PyInstaller pode esquecer e grava relatorios\\autoteste.txt."""
    linhas = [f"LightWireless {__version__}"]
    for mod in ("tinytuya", "pystray", "pystray._win32", "PIL.ImageTk", "tkinter", "winreg",
                "tuya_sharing", "segno", "cryptography.hazmat.primitives.ciphers.aead"):
        try:
            __import__(mod)
            linhas.append(f"ok {mod}")
        except Exception as e:
            linhas.append(f"FALHA {mod}: {type(e).__name__}: {e}")
    try:
        # o 3.5 cifra com AES-GCM: sem a biblioteca certa so falha na hora de conectar
        from tinytuya import AESCipher
        AESCipher(b"0123456789abcdef").encrypt(b"teste", use_base64=False, pad=False, iv=True, header=b"h")
        linhas.append("ok AES-GCM do tinytuya")
    except Exception as e:
        linhas.append(f"FALHA AES-GCM: {type(e).__name__}: {e}")
    try:
        # login por QR: o SDK monta e o QR sai da matriz (sem rede)
        from tuya_sharing import LoginControl, Manager  # noqa: F401
        import segno
        assert len(segno.make("tuyaSmart--qrLogin?token=teste", error="m", micro=False).matrix) > 20
        linhas.append("ok SDK da Tuya + QR")
    except Exception as e:
        linhas.append(f"FALHA SDK/QR: {type(e).__name__}: {e}")
    try:
        import tkinter as tk
        from . import estilo as E, icones
        r = tk.Tk()
        r.withdraw()
        E.iniciar(r)
        E.icone("marca", 32, E.ACENTO)
        icones.desenhar("wifi", 20, "#FFFFFF")
        r.destroy()
        linhas.append("ok tk + icones")
    except Exception as e:
        linhas.append(f"FALHA tk: {type(e).__name__}: {e}")
    (relatorios() / "autoteste.txt").write_text("\n".join(linhas) + "\n", encoding="utf-8")


def main():
    if sys.stdout is None:                       # pythonw / .exe sem console
        import io
        sys.stdout = sys.stderr = io.StringIO()
    if "--autoteste" in sys.argv:
        _autoteste()
        return
    _log()
    if not _unico():
        return
    logging.info("inicio %s %s", __version__, " ".join(sys.argv[1:]))
    estilo.ciente_de_dpi()
    from .config import Config
    from .janela import Janela

    cfg = Config.carregar()
    escondida = "--bandeja" in sys.argv or bool(cfg.opcoes.get("comecar_na_bandeja"))
    j = Janela(cfg, escondida=escondida)
    try:
        j.rodar()
    finally:
        j.encerrar_lampada()
        logging.info("fim")


if __name__ == "__main__":
    main()
