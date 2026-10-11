"""Login na conta Tuya por User Code + QR (tuya-device-sharing-sdk, o mesmo do
Home Assistant) para pegar UMA VEZ o id e a chave local da lampada.

Sem conta de desenvolvedor e sem senha no programa: ele digita o User Code do
app Tuya Smart, escaneia o QR no app e confirma. A nuvem so e usada nesse
momento; depois o programa volta a falar so local com a lampada.

Usa o registro PUBLICO do Home Assistant (client_id/schema abaixo, conferidos
em homeassistant/components/tuya/const.py em 11/out/2026). A Tuya diz que e
provisorio: se um dia parar, "Colar a chave" continua sendo a reserva.

Tudo aqui roda numa thread de trabalho (as chamadas sao HTTP bloqueantes, ate
60 s cada no SDK). A janela so le a fila de eventos. Nada de token, senha ou
user code e gravado; a chave nunca vai pro log (so o tamanho dela).
"""
from __future__ import annotations

import datetime
import logging
import queue
import threading
import time

from .caminhos import relatorios

log = logging.getLogger(__name__)

CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
SCHEMA = "haauthorize"
PREFIXO_QR = "tuyaSmart--qrLogin?token="
INTERVALO_S = 2.0           # consulta do login_result
VALIDADE_QR_S = 90.0        # cada QR; 2 QRs = ~3 min no total
REGERAR_AUTO = 1            # depois disso, botao "Gerar novo QR"
FALHAS_DE_REDE = 3          # seguidas, durante a espera, viram "sem internet"
TAMANHO_DA_CHAVE = 16
CATEGORIA_LAMPADA = "dj"
RELATORIO = "login-tuya.txt"

# codigos do servidor que encerram a espera (o resto = "ainda nao confirmou")
CODIGOS_USER_CODE = {"USERCODE_INCORRECT", "USER_CODE_INCORRECT", "USERCODE_ERROR"}


# == relatorio (sem segredo) ==================================================
def relatar(texto: str) -> None:
    """Linha em relatorios\\login-tuya.txt. NUNCA passar chave/token aqui."""
    try:
        with open(relatorios() / RELATORIO, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {texto}\n")
    except OSError:
        pass


def mascarar(chave: str) -> str:
    return f"[{len(chave or '')} caracteres, oculta]"


def fim(id_: str) -> str:
    return f"…{id_[-6:]}" if id_ else "(sem id)"


# == casamento conta x rede (puro, testavel) ==================================
def chave_valida(chave: str) -> str:
    """'' se serve; senao o motivo em portugues."""
    if not chave:
        return ("A conta não devolveu a chave desse aparelho. Acontece com aparelho "
                "compartilhado por outra conta: entre com a conta dona da lâmpada.")
    if len(chave) != TAMANHO_DA_CHAVE:
        return (f"A chave veio com {len(chave)} caracteres (o normal é {TAMANHO_DA_CHAVE}). "
                "Tente de novo; se repetir, use \"Colar a chave\".")
    return ""


def casar(conta: list[dict], achados: list[dict]) -> tuple[str, object]:
    """Decide o que fazer com a lista da conta.

    -> ("vazia", None)     conta sem aparelho
       ("um", aparelho)    UM aparelho da conta esta nesta rede (ip vem da rede)
       ("escolher", lista) varios, nenhum na rede ou varredura vazia: ele escolhe
    A lista sai com as lampadas (categoria dj) e as da rede primeiro."""
    if not conta:
        return "vazia", None
    ips = {a.get("id"): a.get("ip", "") for a in achados if a.get("id")}
    na_rede = [dict(d, ip=ips[d["id"]], na_rede=True) for d in conta if d["id"] in ips]
    if len(na_rede) == 1:
        return "um", na_rede[0]
    lista = [dict(d, ip=ips.get(d["id"], ""), na_rede=d["id"] in ips) for d in conta]
    lista.sort(key=lambda d: (not d["na_rede"], d.get("categoria") != CATEGORIA_LAMPADA, d.get("nome", "").lower()))
    return "escolher", lista


def _aparelho(item: dict) -> dict | None:
    """Item cru da API (/v1.0/m/life/ha/home/devices) -> o que o programa usa."""
    if not isinstance(item, dict) or not item.get("id"):
        return None
    return {"id": str(item["id"]), "nome": str(item.get("name") or item.get("product_name") or ""),
            "categoria": str(item.get("category") or ""), "chave": str(item.get("local_key") or ""),
            "produto": str(item.get("product_name") or ""), "sub": bool(item.get("sub"))}


# == a sessao (thread de trabalho) =============================================
class Sessao:
    """Uma tentativa de login. Eventos na fila `eventos` (tuplas):
    ("qr", texto, n, validade_s)   QR novo para desenhar (n = 1, 2, ...)
    ("aguardando", resta_s)        (nao emitido: a janela conta sozinha)
    ("buscando",)                  login confirmado, lendo a conta
    ("conta", lista)               aparelhos da conta (dicts de _aparelho)
    ("erro", tipo, texto)          tipo: user_code | expirado | rede | vazia | sdk
    Cancelar a qualquer momento: `cancelar()`; dali em diante nada mais sai."""

    def __init__(self, user_code: str, eventos: queue.Queue, *, sdk=None, dormir=None, agora=None):
        self.user_code = user_code
        self.eventos = eventos
        self._cancelado = threading.Event()
        self._sdk = sdk                     # testes injetam um modulo falso
        self._dormir = dormir or self._cancelado.wait
        self._agora = agora or time.monotonic
        self.thread: threading.Thread | None = None

    # -- controle -------------------------------------------------------------
    def iniciar(self):
        self.thread = threading.Thread(target=self._rodar, name="conta-tuya", daemon=True)
        self.thread.start()
        return self

    def cancelar(self):
        self._cancelado.set()

    @property
    def cancelada(self) -> bool:
        return self._cancelado.is_set()

    def _emitir(self, *ev):
        if not self.cancelada:
            self.eventos.put(ev)

    def _carregar_sdk(self):
        if self._sdk is not None:
            return self._sdk
        import tuya_sharing
        return tuya_sharing

    # -- o trabalho -----------------------------------------------------------
    def _rodar(self):
        try:
            self._trabalhar()
        except Exception as e:
            log.exception("login tuya: erro inesperado")
            relatar(f"erro inesperado do SDK: {type(e).__name__}")
            self._emitir("erro", "sdk", "Algo inesperado aconteceu no login da Tuya. "
                         "Tente de novo; o detalhe ficou no relatório.")

    def _trabalhar(self):
        try:
            sdk = self._carregar_sdk()
        except Exception as e:
            log.exception("login tuya: SDK nao carregou")
            relatar(f"SDK nao carregou: {type(e).__name__}")
            self._emitir("erro", "sdk", "O componente de login da Tuya não carregou neste programa.")
            return
        login = sdk.LoginControl()
        try:
            self._com_login(sdk, login)
        finally:
            try:
                login.session.close()
            except Exception:
                pass

    def _com_login(self, sdk, login):
        info = None
        for n in range(1, REGERAR_AUTO + 2):
            token = self._pedir_qr(login)
            if not token:
                return
            self._emitir("qr", PREFIXO_QR + token, n, VALIDADE_QR_S)
            relatar(f"QR {n} gerado")
            info = self._esperar(login, token)
            if info is None or self.cancelada:
                return                          # erro ja emitido, ou cancelado
            if info is not False:
                break
            relatar(f"QR {n} expirou sem confirmação")
        else:
            self._emitir("erro", "expirado", "O QR expirou sem a confirmação no app. Gere um novo.")
            return
        relatar("login confirmado no app")
        self._emitir("buscando")
        conta = self._ler_conta(sdk, info)
        if conta is None or self.cancelada:
            return
        relatar(f"conta: {len(conta)} aparelho(s); "
                + ", ".join(f"{fim(d['id'])} {d['categoria'] or '?'} chave {mascarar(d['chave'])}" for d in conta))
        if not conta:
            self._emitir("erro", "vazia", "A conta não tem nenhum aparelho. A lâmpada foi cadastrada "
                         "no Tuya Smart com esta conta?")
            return
        self._emitir("conta", conta)

    def _pedir_qr(self, login) -> str:
        try:
            r = login.qr_code(CLIENT_ID, SCHEMA, self.user_code)
        except Exception as e:
            self._erro_rede(e, "pedir o QR")
            return ""
        if not isinstance(r, dict):
            self._emitir("erro", "sdk", "A Tuya respondeu algo que o programa não entende. Tente de novo.")
            return ""
        if not r.get("success"):
            code, msg = str(r.get("code", "")), str(r.get("msg", ""))
            relatar(f"QR recusado: {code} {msg}")
            self._emitir("erro", "user_code",
                         f"A Tuya recusou o User Code ({msg or 'sem mensagem'}"
                         f"{', ' + code if code else ''}). Confira no app e digite de novo.")
            return ""
        token = (r.get("result") or {}).get("qrcode") if isinstance(r.get("result"), dict) else None
        if not token:
            self._emitir("erro", "sdk", "A Tuya não mandou o QR. Tente de novo.")
            return ""
        return str(token)

    def _esperar(self, login, token):
        """dict = login feito; False = QR venceu; None = erro/cancelado."""
        inicio = self._agora()
        falhas = 0
        vistos: set[str] = set()
        while self._agora() - inicio < VALIDADE_QR_S:
            if self._dormir(INTERVALO_S) or self.cancelada:
                return None
            try:
                ok, info = login.login_result(token, CLIENT_ID, self.user_code)
            except Exception as e:
                falhas += 1
                log.warning("login tuya: consulta falhou (%s)", type(e).__name__)
                if falhas >= FALHAS_DE_REDE:
                    self._erro_rede(e, "esperar a confirmação")
                    return None
                continue
            falhas = 0
            if ok and isinstance(info, dict):
                if not (info.get("terminal_id") and info.get("endpoint") and info.get("access_token")):
                    self._emitir("erro", "sdk", "O login voltou incompleto da Tuya. Tente de novo.")
                    relatar("login sem terminal_id/endpoint/token")
                    return None
                return info
            code = str((info or {}).get("code", "")) if isinstance(info, dict) else ""
            if code and code not in vistos:      # aprender os codigos reais da espera
                vistos.add(code)
                relatar(f"espera: servidor disse {code} {(info or {}).get('msg', '')}")
            if code in CODIGOS_USER_CODE:
                self._emitir("erro", "user_code", "A Tuya recusou o User Code durante a espera. "
                             "Confira no app e digite de novo.")
                return None
        return False

    def _ler_conta(self, sdk, info) -> list[dict] | None:
        mgr = None
        try:
            mgr = sdk.Manager(CLIENT_ID, self.user_code, info["terminal_id"], info["endpoint"], info, None)
            casas = mgr.home_repository.query_homes()
            lista: list[dict] = []
            vistos = set()
            # direto na API das casas (a mesma do update_device_cache), sem as
            # 3 consultas extras por aparelho que o HA faz e nao servem aqui
            for casa in casas or []:
                if self.cancelada:
                    return None
                r = mgr.customer_api.get("/v1.0/m/life/ha/home/devices", {"homeId": casa.id})
                if not isinstance(r, dict) or not r.get("success"):
                    raise RuntimeError("lista de aparelhos sem sucesso")
                for item in r.get("result") or []:
                    d = _aparelho(item)
                    if d and d["id"] not in vistos:
                        vistos.add(d["id"])
                        lista.append(d)
            return lista
        except Exception as e:
            if _parece_rede(e):
                self._erro_rede(e, "ler a conta")
            else:
                log.exception("login tuya: leitura da conta")
                relatar(f"leitura da conta falhou: {type(e).__name__}")
                self._emitir("erro", "sdk", "O login deu certo, mas a lista de aparelhos não veio. Tente de novo.")
            return None
        finally:
            _descarregar(mgr)

    def _erro_rede(self, e, onde):
        log.warning("login tuya: sem rede ao %s (%s)", onde, type(e).__name__)
        relatar(f"sem rede ao {onde}: {type(e).__name__}")
        if _parece_rede(e):
            self._emitir("erro", "rede", "Sem conexão com a Tuya. O PC está na internet? Tente de novo.")
        else:
            self._emitir("erro", "sdk", "A Tuya não respondeu como esperado. Tente de novo; "
                         "o detalhe ficou no relatório.")


def _parece_rede(e: Exception) -> bool:
    try:
        import requests
        if isinstance(e, requests.exceptions.RequestException):
            return True
    except Exception:
        pass
    return isinstance(e, (OSError, TimeoutError, ConnectionError))


def _descarregar(mgr) -> None:
    """Expira o token do terminal (o login some da conta) e fecha as sessoes
    HTTP. MQTT nunca e ligado (refresh_mq nao e chamado): nenhuma thread fica."""
    if mgr is None:
        return
    try:
        mgr.unload()
    except Exception as e:
        log.info("login tuya: unload falhou (%s), sem importancia", type(e).__name__)
    try:
        mgr.customer_api.session.close()
    except Exception:
        pass
