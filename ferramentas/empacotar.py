"""Gera o .exe portatil (pasta) e o zip do release, e roda o autoteste do .exe.

Uso: python ferramentas\\empacotar.py
Sai em dist\\LightWireless\\ (LightWireless.exe + _internal\\) e
dist\\LightWireless-<versao>-windows.zip (o que vai no Release do GitHub).

ATUALIZA NO LUGAR (pedido dele, 11/out/2026): o PyInstaller gera numa pasta
temporaria e so o que MUDOU e copiado para dist\\LightWireless; o que sumiu do
build sai so de dentro de _internal\\. config.json, .bak, relatorios\\ e o que o
programa cria (ferramentas\\) nunca sao movidos nem apagados.
FECHA E REABRE (permissao dele, 11/out/2026): se o .exe de dist\\ estiver
aberto, pede para sair (relatorios\\.sair: sai SEM a acao "ao sair" -- build nao
mexe na luz); versao antiga que nao atende e encerrada a forca. Reabre no fim.

MODO PASTA, nao arquivo unico: o programa sobe com o Windows, e o arquivo
unico descompacta tudo num temporario a cada abertura.
SEGREDO: config.json/devices.json/tinytuya.json NUNCA entram no zip (levam a
chave da lampada). O icone e desenhado na hora (icones.marca), nao versionado.
"""
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
from lightwireless import __version__, icones  # noqa: E402

NOME = "LightWireless"
SEGREDOS = ("config.json", "config.json.bak", "config.json.tmp", "devices.json", "tinytuya.json")
# criados pelo PROGRAMA na pasta dele: ficam em dist\ e nunca vao no zip
DO_USUARIO = ("relatorios", "ferramentas")


# == fechar / reabrir o .exe de dist\ ===========================================
def _pids(exe: str) -> list[int]:
    """Processos rodando ESTE .exe (o instalado em outra pasta nao conta)."""
    cmd = (f"Get-Process -Name '{NOME}' -ErrorAction SilentlyContinue | "
           f"Where-Object {{ $_.Path -eq '{os.path.abspath(exe)}' }} | ForEach-Object {{ $_.Id }}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                           encoding="oem", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in r.stdout.split() if x.isdigit()]


def fechar(pasta: str) -> bool:
    """Fecha o .exe de dist\\ se aberto. Devolve se estava aberto (para reabrir)."""
    exe = os.path.join(pasta, NOME + ".exe")
    if not _pids(exe):
        return False
    rel = os.path.join(pasta, "relatorios")
    os.makedirs(rel, exist_ok=True)
    with open(os.path.join(rel, ".sair"), "w") as f:
        f.write("build")
    fim = time.monotonic() + 8
    while time.monotonic() < fim and _pids(exe):
        time.sleep(0.5)
    if _pids(exe):                               # versao antiga, sem o .sair
        print("o .exe aberto nao atendeu o pedido: encerrando a forca")
        for pid in _pids(exe):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        time.sleep(1.0)
    try:
        os.remove(os.path.join(rel, ".sair"))
    except OSError:
        pass
    print("fechado para o build")
    return True


# == atualizar no lugar ===========================================================
def sincronizar(novo: str, destino: str) -> tuple[int, int]:
    """Copia de `novo` so o que mudou; tira de _internal\\ o que o build nao tem mais."""
    copiados = removidos = 0
    for raiz, _dirs, arqs in os.walk(novo):
        rel_dir = os.path.relpath(raiz, novo)
        os.makedirs(os.path.join(destino, rel_dir), exist_ok=True)
        for a in arqs:
            o, d = os.path.join(raiz, a), os.path.join(destino, rel_dir, a)
            if not os.path.exists(d) or not filecmp.cmp(o, d, shallow=False):
                shutil.copy2(o, d)
                copiados += 1
    interno_d, interno_n = os.path.join(destino, "_internal"), os.path.join(novo, "_internal")
    for raiz, dirs, arqs in os.walk(interno_d, topdown=False):
        rel_dir = os.path.relpath(raiz, interno_d)
        for a in arqs:
            if not os.path.exists(os.path.join(interno_n, rel_dir, a)):
                os.remove(os.path.join(raiz, a))
                removidos += 1
        if not os.listdir(raiz) and raiz != interno_d:
            os.rmdir(raiz)
    return copiados, removidos


def main():
    os.chdir(RAIZ)
    pasta_exe = os.path.join("dist", NOME)
    exe = os.path.join(pasta_exe, NOME + ".exe")
    # build\, .spec, .ico e a saida do PyInstaller numa pasta temporaria apagada
    # no fim: a pasta do projeto fica so com o que e usado (pedido dele, 10/out/2026)
    temp = tempfile.mkdtemp(prefix="lightwireless_build_")
    reabrir = False
    try:
        ico = icones.gravar_ico(os.path.join(temp, "marca.ico"))
        args = [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
            "--workpath", os.path.join(temp, "work"), "--specpath", temp,
            "--distpath", os.path.join(temp, "dist"), "--name", NOME, "--icon", ico,
            # o tinytuya escolhe a biblioteca de AES em tempo de execucao: sem ela o
            # 3.5 so falha na hora de conectar (o autoteste confere)
            "--collect-submodules", "tinytuya", "--exclude-module", "tinytuya.Contrib",
            "--hidden-import", "pystray._win32", "--hidden-import", "PIL._tkinter_finder",
            # login por QR: o SDK da Tuya (importado so na hora, dentro da thread) e
            # o cryptography que ele usa sem declarar; segno desenha o QR
            "--collect-submodules", "tuya_sharing", "--hidden-import", "segno",
            "--hidden-import", "cryptography.hazmat.primitives.ciphers.aead",
            "--exclude-module", "PyInstaller",
            # puxados por dependencias e sem uso aqui (pesam dezenas de MB)
            "--exclude-module", "numpy", "--exclude-module", "charset_normalizer",
            "--exclude-module", "setuptools", "--exclude-module", "pkg_resources",
            "--exclude-module", "matplotlib", "--exclude-module", "PIL.ImageQt",
            os.path.abspath("LightWireless.pyw"),
        ]
        subprocess.run(args, check=True)       # o programa dele segue aberto enquanto compila
        novo = os.path.join(temp, "dist", NOME)
        for arq, dest in (("README.md", "README.md"), ("LICENSE", "LICENSE.txt"),
                          ("THIRD-PARTY-NOTICES.txt", "THIRD-PARTY-NOTICES.txt")):
            shutil.copy(arq, os.path.join(novo, dest))   # a GPLv3 exige a licenca junto do binario
        reabrir = fechar(pasta_exe)
        copiados, removidos = sincronizar(novo, pasta_exe)
        print(f"dist\\{NOME} atualizado no lugar: {copiados} arquivo(s) trocado(s), {removidos} removido(s)")
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        for raiz, dirs, _arqs in os.walk(RAIZ):
            if "__pycache__" in dirs:
                shutil.rmtree(os.path.join(raiz, "__pycache__"), ignore_errors=True)

    # autoteste do .exe: carrega o que o PyInstaller costuma esquecer
    subprocess.run([exe, "--autoteste"], timeout=60)
    rel = os.path.join(pasta_exe, "relatorios", "autoteste.txt")
    texto = open(rel, encoding="utf-8").read() if os.path.exists(rel) else "SEM RELATORIO\n"
    print(texto)

    zipado = os.path.join("dist", f"{NOME}-{__version__}-windows.zip")
    if os.path.exists(zipado):
        os.remove(zipado)
    with zipfile.ZipFile(zipado, "w", zipfile.ZIP_DEFLATED) as z:
        for raiz, _dirs, arqs in os.walk(pasta_exe):
            for a in arqs:
                caminho = os.path.join(raiz, a)
                rel_ = os.path.relpath(caminho, pasta_exe)
                if rel_ in SEGREDOS or rel_.split(os.sep)[0] in DO_USUARIO:
                    continue                  # chave e o que o programa criou na maquina dele
                z.write(caminho, os.path.relpath(caminho, "dist"))
        nomes = z.namelist()
    vazou = [n for n in nomes if os.path.basename(n) in SEGREDOS]
    print(f"pronto: {exe} e {zipado} ({os.path.getsize(zipado) // 1024} KB, {len(nomes)} arquivos)")
    if reabrir:
        subprocess.Popen([os.path.abspath(exe)], cwd=os.path.abspath(pasta_exe))
        print("reaberto")
    if vazou or "FALHA" in texto:
        print("ATENCAO:", "segredo no zip: %s" % vazou if vazou else "autoteste com FALHA")
        sys.exit(1)


if __name__ == "__main__":
    main()
