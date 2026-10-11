"""Testes do login por conta Tuya (User Code + QR) SEM rede e SEM a chave dele.

Uso: python ferramentas\\testar_conta_tuya.py [--real]
  sem argumento: SDK simulado (LoginControl/Manager falsos), logica + janela
  --real: tambem chama o SDK de verdade com um User Code FALSO (precisa de
          internet; tem que voltar success False sem excecao)

Roda numa COPIA do pacote numa pasta temporaria: config.json e relatorios\\
de la nascem vazios (nada da lampada dele e lido ou gravado). Imprime so o
resumo; sai 1 se algo falhar.
"""
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import types

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP = tempfile.mkdtemp(prefix="lw_teste_conta_")
shutil.copytree(os.path.join(RAIZ, "lightwireless"), os.path.join(TEMP, "lightwireless"),
                ignore=shutil.ignore_patterns("__pycache__"))
sys.path.insert(0, TEMP)
sys.dont_write_bytecode = True

from lightwireless import conta_tuya as CT  # noqa: E402

RES = []
CHAVE_BOA = "Ab3dEf6hIj9lMn2p"          # falsa, 16 caracteres


def conferir(nome, cond, detalhe=""):
    RES.append((nome, bool(cond), detalhe))


# == SDK falso =================================================================
class ErroRede(Exception):
    pass


try:
    import requests
    ErroRede = requests.exceptions.ConnectionError   # noqa: F811 (o que o SDK levanta de verdade)
except Exception:
    pass


def sdk_falso(qr=None, resultados=None, casas=("casa1",), aparelhos=None, erro_manager=None,
              atraso_resultado=0.0, erro_lista=None):
    """qr: dict devolvido pelo qr_code (ou excecao a levantar);
    resultados: lista de (ok, info) ou excecoes, consumida a cada login_result
    (acabou = repete "ainda nao confirmou")."""
    est = types.SimpleNamespace(qr_pedidos=0, consultas=0, unload=0, fechou=0)
    qr = qr if qr is not None else {"success": True, "result": {"qrcode": "TOKEN"}}
    fila = list(resultados or [])
    aparelhos = aparelhos if aparelhos is not None else []

    class LoginControl:
        def qr_code(self, cid, schema, uc):
            assert cid == CT.CLIENT_ID and schema == CT.SCHEMA
            est.qr_pedidos += 1
            if isinstance(qr, Exception):
                raise qr
            r = dict(qr)
            if r.get("success"):
                r["result"] = {"qrcode": f"TOKEN{est.qr_pedidos}"}
            return r

        def login_result(self, token, cid, uc):
            est.consultas += 1
            if atraso_resultado:
                time.sleep(atraso_resultado)
            if fila:
                r = fila.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
            return False, {"success": False, "code": "AGUARDANDO", "msg": "ainda nao"}

    class Manager:
        def __init__(self, cid, uc, terminal, endpoint, info, listener):
            if erro_manager:
                raise erro_manager
            assert terminal == "T1" and endpoint.startswith("https://")
            self.home_repository = types.SimpleNamespace(
                query_homes=lambda: [types.SimpleNamespace(id=c, name=c) for c in casas])
            api = types.SimpleNamespace(session=types.SimpleNamespace(close=self._fechar))

            def get(path, params):
                if erro_lista:
                    raise erro_lista
                assert path == "/v1.0/m/life/ha/home/devices"
                return {"success": True, "result": [a for a in aparelhos if a.get("_casa", casas[0]) == params["homeId"]]}
            api.get = get
            self.customer_api = api

        def _fechar(self):
            est.fechou += 1

        def unload(self):
            est.unload += 1

    return types.SimpleNamespace(LoginControl=LoginControl, Manager=Manager), est


OK_LOGIN = (True, {"t": 1, "uid": "u", "expire_time": 7200, "access_token": "AT", "refresh_token": "RT",
                   "terminal_id": "T1", "endpoint": "https://apigw.tuyaus.com", "username": "x"})


def lamp(id_, chave=CHAVE_BOA, cat="dj", nome="15W smart color"):
    return {"id": id_, "name": nome, "category": cat, "local_key": chave, "product_name": nome, "sub": False}


def rodar(sdk, cancelar_apos=None):
    """Roda a sessao NA HORA (sem thread), com relogio falso."""
    relogio = [0.0]
    n = [0]
    q = queue.Queue()
    s = CT.Sessao("UCFALSO", q, sdk=sdk, agora=lambda: relogio[0])

    def dormir(seg):
        relogio[0] += seg
        n[0] += 1
        if cancelar_apos is not None and n[0] >= cancelar_apos:
            s.cancelar()
        return s.cancelada
    s._dormir = dormir
    s._rodar()
    evs = []
    while not q.empty():
        evs.append(q.get())
    return evs, s


def tipos(evs):
    return [e[0] + (":" + e[1] if e[0] == "erro" else "") for e in evs]


# == A. logica com o SDK simulado ================================================
def testes_logica():
    sdk, est = sdk_falso(resultados=[(False, {"code": "W"}), (False, {"code": "W"}), OK_LOGIN],
                         aparelhos=[lamp("bf0000aaaabbbbcccc1234")])
    evs, _ = rodar(sdk)
    conferir("sucesso: qr -> buscando -> conta", tipos(evs) == ["qr", "buscando", "conta"], tipos(evs))
    conferir("sucesso: texto do QR", evs[0][1] == CT.PREFIXO_QR + "TOKEN1", evs[0][1])
    conferir("sucesso: conta com a chave", evs[-1][1][0]["chave"] == CHAVE_BOA)
    conferir("sucesso: Manager descarregado (unload + sessao fechada)", est.unload == 1 and est.fechou == 1)

    sdk, est = sdk_falso(qr={"success": False, "code": "USERCODE_INCORRECT", "msg": "User Code Incorrect"})
    evs, _ = rodar(sdk)
    conferir("user code errado -> erro user_code com msg/code", tipos(evs) == ["erro:user_code"]
             and "User Code Incorrect" in evs[0][2] and "USERCODE_INCORRECT" in evs[0][2], evs)

    n_qr = int(CT.VALIDADE_QR_S / CT.INTERVALO_S)
    sdk, est = sdk_falso(resultados=[(False, {"code": "W"})] * n_qr + [OK_LOGIN], aparelhos=[lamp("a1")])
    evs, _ = rodar(sdk)
    conferir("QR expirado -> QR novo automatico -> sucesso",
             tipos(evs) == ["qr", "qr", "buscando", "conta"] and evs[1][2] == 2 and est.qr_pedidos == 2, tipos(evs))

    sdk, est = sdk_falso()
    evs, _ = rodar(sdk)
    conferir("timeout total -> erro expirado (2 QRs, ~3 min)",
             tipos(evs) == ["qr", "qr", "erro:expirado"] and est.qr_pedidos == 1 + CT.REGERAR_AUTO,
             (tipos(evs), est.consultas))

    sdk, est = sdk_falso()
    evs, s = rodar(sdk, cancelar_apos=3)
    conferir("cancelar na espera -> para e nao emite mais nada",
             tipos(evs) == ["qr"] and est.consultas == 2 and s.cancelada, (tipos(evs), est.consultas))

    sdk, est = sdk_falso(resultados=[OK_LOGIN], aparelhos=[])
    evs, _ = rodar(sdk)
    conferir("conta sem aparelho -> erro vazia", tipos(evs) == ["qr", "buscando", "erro:vazia"], tipos(evs))

    sdk, est = sdk_falso(qr=ErroRede("sem rede"))
    evs, _ = rodar(sdk)
    conferir("rede caida ao pedir o QR -> erro rede", tipos(evs) == ["erro:rede"], tipos(evs))

    sdk, est = sdk_falso(resultados=[ErroRede("x")] * 5)
    evs, _ = rodar(sdk)
    conferir("rede caida na espera (3 seguidas) -> erro rede",
             tipos(evs) == ["qr", "erro:rede"] and est.consultas == CT.FALHAS_DE_REDE, (tipos(evs), est.consultas))

    sdk, est = sdk_falso(resultados=[ErroRede("x"), (False, {"code": "W"}), ErroRede("x"), OK_LOGIN],
                         aparelhos=[lamp("a1")])
    evs, _ = rodar(sdk)
    conferir("falha de rede isolada na espera nao derruba", tipos(evs)[-1] == "conta", tipos(evs))

    sdk, est = sdk_falso(resultados=[OK_LOGIN], erro_manager=ValueError("quebrou"))
    evs, _ = rodar(sdk)
    conferir("excecao do SDK (Manager) -> erro sdk", tipos(evs) == ["qr", "buscando", "erro:sdk"], tipos(evs))

    sdk, est = sdk_falso(resultados=[OK_LOGIN], aparelhos=[lamp("a1")], erro_lista=KeyError("result"))
    evs, _ = rodar(sdk)
    conferir("excecao do SDK (lista) -> erro sdk + unload mesmo assim",
             tipos(evs)[-1] == "erro:sdk" and est.unload == 1, (tipos(evs), est.unload))

    sdk, est = sdk_falso(resultados=[OK_LOGIN], erro_lista=ErroRede("x"))
    evs, _ = rodar(sdk)
    conferir("rede caida ao ler a conta -> erro rede", tipos(evs)[-1] == "erro:rede", tipos(evs))

    sdk, est = sdk_falso(resultados=[(True, {"t": 1})])
    evs, _ = rodar(sdk)
    conferir("login incompleto -> erro sdk", tipos(evs) == ["qr", "erro:sdk"], tipos(evs))

    sdk, est = sdk_falso(qr={"success": True, "result": {}})
    sdk.LoginControl.qr_code = lambda self, *a: {"success": True, "result": {}}
    evs, _ = rodar(sdk)
    conferir("QR sem token -> erro sdk", tipos(evs) == ["erro:sdk"], tipos(evs))

    class Quebra:
        def __getattr__(self, n):
            raise ImportError("sem modulo")
    evs, _ = rodar(Quebra())
    conferir("SDK ausente/quebrado -> erro sdk, sem excecao", tipos(evs) == ["erro:sdk"], tipos(evs))

    # duas casas, aparelho repetido nao duplica
    sdk, est = sdk_falso(resultados=[OK_LOGIN], casas=("c1", "c2"),
                         aparelhos=[dict(lamp("a1"), _casa="c1"), dict(lamp("b2", cat="cz", nome="Tomada"), _casa="c2"),
                                    dict(lamp("a1"), _casa="c2")])
    evs, _ = rodar(sdk)
    conferir("varias casas, sem duplicar", tipos(evs)[-1] == "conta" and [d["id"] for d in evs[-1][1]] == ["a1", "b2"],
             evs[-1])

    # casamento
    conta = [CT._aparelho(lamp("a1")), CT._aparelho(lamp("b2", cat="cz", nome="Tomada")),
             CT._aparelho(lamp("c3", nome="Abajur"))]
    rede = [{"id": "a1", "ip": "192.168.0.20"}]
    m, r = CT.casar(conta, rede)
    conferir("id que casa -> 'um' com o IP da rede", m == "um" and r["id"] == "a1" and r["ip"] == "192.168.0.20", (m, r))
    m, r = CT.casar(conta, [{"id": "zz", "ip": "1.2.3.4"}])
    conferir("id que nao casa -> escolher, lampadas primeiro",
             m == "escolher" and [d["id"] for d in r] == ["a1", "c3", "b2"], (m, [d["id"] for d in r]))
    m, r = CT.casar(conta, [])
    conferir("varredura vazia -> escolher", m == "escolher" and len(r) == 3)
    m, r = CT.casar([conta[0]], [])
    conferir("1 aparelho e varredura vazia -> escolher (confirma)", m == "escolher" and len(r) == 1)
    m, r = CT.casar(conta, [{"id": "a1", "ip": "x"}, {"id": "c3", "ip": "y"}])
    conferir("dois da conta na rede -> escolher, os da rede primeiro",
             m == "escolher" and {r[0]["id"], r[1]["id"]} == {"a1", "c3"} and r[0]["na_rede"], [d["id"] for d in r])
    m, r = CT.casar([], rede)
    conferir("conta vazia -> vazia", m == "vazia")
    conferir("chave vazia -> motivo (compartilhado)", "compartilhado" in CT.chave_valida(""))
    conferir("chave curta -> motivo com tamanho", "10 caracteres" in CT.chave_valida("0123456789"))
    conferir("chave de 16 -> aceita", CT.chave_valida(CHAVE_BOA) == "")
    conferir("mascarar nao mostra a chave", CHAVE_BOA not in CT.mascarar(CHAVE_BOA))


# == B. janela (sem rede, SDK simulado na thread de verdade) =======================
def testes_janela():
    from lightwireless import bandeja, descoberta, estilo, lampada as L
    from lightwireless import janela as JN
    estilo.ciente_de_dpi()
    bandeja.Bandeja.iniciar = lambda self: False
    descoberta.procurar_insistindo = lambda ao_tentar=None, **k: (
        time.sleep(0.2) or [{"ip": "192.168.0.21", "id": "lampadaDaRede01", "versao": "3.5"}])
    from lightwireless.config import Config
    JN.Janela._posicionar = lambda self: self.raiz.geometry("+-3000+0")

    class Falsa:
        def __init__(s): s.iniciadas = 0
        def iniciar(s): s.iniciadas += 1
        def encerrar(s, espera_s=0): pass
        def __getattr__(s, n): return lambda *a, **k: None
    criadas = []

    def nova(self):
        criadas.append(Falsa())
        return criadas[-1]
    JN.Janela._nova_lampada = nova
    erros = []
    cfg = Config.carregar()
    j = JN.Janela(cfg, lampada=Falsa())
    j.raiz.report_callback_exception = lambda *e: erros.append(e)
    controle = {"liberar": threading.Event()}
    atual = {}

    def usar(sdk):
        CT.Sessao._carregar_sdk = lambda self: sdk
        atual["sdk"] = sdk

    def girar(seg, ate=None):
        """Roda o laco do Tk por `seg` s (ou ate `ate()`); devolve o maior vao entre voltas."""
        t0 = time.monotonic()
        maior, ult = 0.0, t0
        while time.monotonic() - t0 < seg:
            j.raiz.update()
            agora = time.monotonic()
            maior, ult = max(maior, agora - ult), agora
            if ate and ate():
                break
            time.sleep(0.01)
        return maior

    CT.INTERVALO_S = 0.05                       # espera curta no teste (a logica e a mesma)
    girar(1.0)

    # 1) caminho feliz: User Code -> QR -> aguardando (login_result trava 0,4 s: Tk segue) -> gravado
    sdk, est = sdk_falso(resultados=[(False, {"code": "W"})] * 3 + [OK_LOGIN],
                         aparelhos=[lamp("lampadaDaRede01"), lamp("outra000000001", cat="cz", nome="Tomada")],
                         atraso_resultado=0.4)
    usar(sdk)
    j.ir("lampada")
    girar(0.5, lambda: not j.cl["procurando"])
    j._conta_abrir("lampada")
    conferir("tela: abre em digitando", j._tela == "lampada_conta" and j.lg["estado"] == "digitando")
    j.e_user_code.insert(0, "  UC 123 ")
    j._conta_gerar()
    sess1 = j._sessao
    j._conta_gerar()                             # clique duplo
    conferir("clique duplo nao cria 2a sessao", j._sessao is sess1 and j.lg["estado"] == "pedindo")
    conferir("user code sem espacos", j.lg["user_code"] == "UC123")
    vao = girar(5, lambda: j.lg["estado"] == "aguardando")
    conferir("tela: QR desenhado", j.lg["estado"] == "aguardando" and j.lg["qr"].startswith(CT.PREFIXO_QR))
    vao = girar(10, lambda: j.lg["estado"] == "gravado")
    conferir("Tk responsivo durante a espera (maior vao < 0,25 s)", vao < 0.25, f"{vao:.3f} s")
    conferir("casou 1 na rede -> gravado direto", j.lg["estado"] == "gravado", j.lg["estado"])
    conferir("config: id + chave + IP da varredura", j.cfg["id"] == "lampadaDaRede01" and j.cfg["chave"] == CHAVE_BOA
             and j.cfg["ip"] == "192.168.0.21", (j.cfg["id"], j.cfg["ip"]))
    conferir("lampada nova iniciada", criadas and criadas[-1].iniciadas == 1)
    conferir("user code nao fica na memoria", j.lg["user_code"] == "")
    import json
    from lightwireless.config import ARQUIVO
    txt = ARQUIVO.read_text(encoding="utf-8")
    conferir("config.json sem token nem user code",
             all(x not in txt for x in ('"AT"', '"RT"', "UC123", "access_token", "refresh_token", "terminal")))
    rel = (CT.relatorios() / CT.RELATORIO).read_text(encoding="utf-8")
    log_ = (CT.relatorios() / "lightwireless.log")
    conferir("relatorio login-tuya.txt sem a chave", CHAVE_BOA not in rel and "gravado" in rel)
    conferir("log sem a chave", not log_.exists() or CHAVE_BOA not in log_.read_text(encoding="utf-8"))

    # 2) .bak da versao anterior + varios aparelhos -> escolher -> chave vazia recusada -> outro aceito
    sdk, est = sdk_falso(resultados=[OK_LOGIN],
                         aparelhos=[lamp("x1", chave="", nome="Compartilhada"), lamp("x2", nome="Abajur")])
    usar(sdk)
    j._conta_abrir("opcoes")
    j.e_user_code.insert(0, "UC")
    j._conta_gerar()
    girar(5, lambda: j.lg["estado"] == "escolher")
    conferir("nenhum na rede -> lista para escolher", j.lg["estado"] == "escolher" and len(j.lg["conta"]) == 2)
    x1 = next(d for d in j.lg["conta"] if d["id"] == "x1")
    j._conta_usar(x1)
    girar(0.2)
    conferir("chave vazia recusada com motivo, nada gravado",
             j.lg["estado"] == "escolher" and "compartilhado" in j.lg["erro"][1] and j.cfg["id"] == "lampadaDaRede01")
    j._conta_usar(next(d for d in j.lg["conta"] if d["id"] == "x2"))
    girar(0.2)
    conferir("escolhido gravado", j.lg["estado"] == "gravado" and j.cfg["id"] == "x2")
    bak = json.loads((ARQUIVO.parent / (ARQUIVO.name + ".bak")).read_text(encoding="utf-8"))
    conferir(".bak tem a versao anterior", bak.get("id") == "lampadaDaRede01")
    conferir("voltar leva para Opções (Atualizar chave)", j.lg["voltar"] == "opcoes")

    # 3) cancelar na espera: sessao larga, eventos atrasados ignorados
    sdk, est = sdk_falso(atraso_resultado=0.3)
    usar(sdk)
    j._conta_abrir("lampada")
    j.e_user_code.insert(0, "UC")
    j._conta_gerar()
    girar(3, lambda: j.lg["estado"] == "aguardando")
    s = j._sessao
    j._conta_cancelar_tela()
    girar(0.8)
    conferir("cancelar -> digitando e sessao cancelada", j.lg["estado"] == "digitando" and s.cancelada and j._sessao is None)
    s.thread.join(2)
    conferir("thread do login terminou", not s.thread.is_alive())

    # 4) user code errado na tela, depois erro de rede, depois expirado com botao
    sdk, _ = sdk_falso(qr={"success": False, "code": "USERCODE_INCORRECT", "msg": "User Code Incorrect"})
    usar(sdk)
    j.e_user_code.insert(0, "ERRADO")
    j._conta_gerar()
    girar(3, lambda: j.lg["estado"] == "erro")
    conferir("user code errado -> erro na tela, campo de volta", j.lg["erro"][0] == "user_code"
             and j.e_user_code.winfo_exists())
    sdk, _ = sdk_falso(qr=ErroRede("x"))
    usar(sdk)
    j._conta_gerar()
    girar(3, lambda: j.lg["erro"][0] == "rede")
    conferir("sem internet -> erro rede", j.lg["erro"][0] == "rede")
    j.lg.update(estado="erro", erro=("expirado", "O QR expirou."))
    j.ir("lampada_conta")
    conferir("expirado -> botao Gerar novo QR", j.b_gerar.texto == "Gerar novo QR")
    j.ir("lampada")
    for st in ("pedindo", "buscando"):
        j.lg["estado"] = st
        j._tela = None
        j.ir("lampada_conta")
        conferir(f"tela monta no estado {st}", j._tela == "lampada_conta")
    j._conta_cancelar()

    # 5) sair com login em curso nao trava
    sdk, _ = sdk_falso(atraso_resultado=0.5)
    usar(sdk)
    j._conta_abrir("lampada")
    j.e_user_code.insert(0, "UC")
    j._conta_gerar()
    girar(2, lambda: j.lg["estado"] == "aguardando")
    s = j._sessao
    t0 = time.monotonic()
    j.motor.encerrar()
    j._conta_cancelar()
    conferir("fechar cancela o login", s.cancelada and time.monotonic() - t0 < 0.5)
    conferir("erros na interface: 0", not erros, erros[:1])
    j.raiz.destroy()


def teste_real():
    try:
        from tuya_sharing import LoginControl
        r = LoginControl().qr_code(CT.CLIENT_ID, CT.SCHEMA, "codigoFalso123")
        conferir("SDK real: user code falso -> success False sem excecao",
                 isinstance(r, dict) and r.get("success") is False, {k: r.get(k) for k in ("code", "msg")})
    except Exception as e:
        conferir("SDK real: user code falso -> success False sem excecao", False, type(e).__name__)


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)            # os erros simulados sao esperados; so o resumo
    try:
        __import__("tuya_sharing")
        conferir("SDK importa neste Python", True, sys.version.split()[0])
    except Exception as e:
        conferir("SDK importa neste Python", False, type(e).__name__)
    testes_logica()
    testes_janela()
    if "--real" in sys.argv:
        teste_real()
    falhas = [r for r in RES if not r[1]]
    for nome, ok, det in RES:
        if not ok:
            print(f"FALHOU  {nome}  {det}")
    print(f"{len(RES) - len(falhas)}/{len(RES)} ok")
    shutil.rmtree(TEMP, ignore_errors=True)
    sys.exit(1 if falhas else 0)
