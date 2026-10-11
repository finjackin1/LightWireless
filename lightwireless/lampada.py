"""
A camada que fala com a lampada. Toda a rede vive aqui.

POR QUE UMA THREAD
------------------
Um comando para a lampada leva de dezenas de milissegundos a alguns segundos
-- e no pior caso a varredura da rede leva ate cinco. Chamado direto do
clique, isso congela a janela: o Tk so redesenha entre um evento e outro.

Entao a janela nunca fala com a rede. Ela empurra o pedido numa fila e volta
a desenhar na mesma hora; a thread executa e avisa o resultado depois. O
botao responde ao toque antes de a lampada ter feito qualquer coisa, que e
como todo controle remoto se comporta.

QUEM CHAMA OS AVISOS
--------------------
Os avisos (`ao_estado`, `ao_conexao`) saem DA THREAD. Tocar em widget do Tk
de outra thread trava ou corrompe a tela, as vezes so depois de horas. Por
isso existe o parametro `agendar`: a janela passa algo como
`lambda f: janela.after(0, f)` e os avisos voltam para a thread da interface.
Sem `agendar`, o aviso e chamado direto -- o que serve para script de teste,
nao para janela.

CONEXAO PERSISTENTE
-------------------
O socket fica aberto entre um comando e outro. Reabrir a cada vez pagaria o
aperto de mao do protocolo 3.5 toda vez, que e a maior parte da demora. Se
algo falha, a conexao e marcada como perdida e refeita no proximo ciclo.
"""

from __future__ import annotations

import logging
import queue
import select
import threading
import time
from typing import Callable

from .config import Config

log = logging.getLogger(__name__)

# Os DPs desta lampada, confirmados pelo wizard do tinytuya. A tabela vem
# inteira de proposito: `DP_BRILHO` ainda nao e usado (brilho ficou fora da
# v1), mas apagar um degrau deixaria o mapa do protocolo incompleto para
# quem for ligar a proxima funcao.
DP_LIGADO = "20"        # switch_led       booleano
DP_MODO = "21"          # work_mode        white / colour / scene / music
DP_BRILHO = "22"        # bright_value_v2  10 a 1000  (ainda sem uso)
DP_TEMPERATURA = "23"   # temp_value_v2    0 a 1000

TEMP_MIN = 0
TEMP_MAX = 1000

# ACOMPANHAR O QUE MUDA POR FORA (app do celular, outro PC, interruptor)
# --------------------------------------------------------------------
# Com o socket aberto, a lampada AVISA sozinha cada mudanca (mensagem de
# status). A thread escuta o socket entre um pedido e outro (select de
# ESCUTA_S: comando novo espera no maximo isso) e repassa na hora -- e o que
# faz o app Tuya acompanhar. (11/out/2026, pedido dele; antes era so a
# releitura de 20 em 20 s.)
ESCUTA_S = 0.05
# Releitura de reserva: pega o que um aviso perdido deixou para tras e mantem
# o socket vivo (a lampada fecha conexao calada depois de ~30 s).
INTERVALO_SONDAGEM_S = 15.0

# Espera antes de tentar reconectar depois de uma falha. Sem isso, lampada
# fora do ar vira laco apertado de tentativa e erro queimando CPU.
ESPERA_RECONEXAO_S = 5.0

# QUANTO ESPERAR PELO SOCKET
# --------------------------
# O tinytuya vem com 5 s de espera, 5 tentativas e 5 s entre elas: quase 50
# segundos preso num IP que nao responde. Isso travou o primeiro teste desta
# camada -- a tentativa no IP em cache nunca voltava, e a busca na rede, que
# era o plano B, nunca chegava a acontecer.
#
# Aqui sao dois perfis. No IP em cache, falhar rapido e o que interessa: o IP
# e so um palpite e existe um plano B logo atras. No IP que a rede acabou de
# confirmar, vale insistir um pouco -- ali a lampada existe mesmo, e a falha
# tende a ser passageira.
ESPERA_CACHE_S = 2.0
TENTATIVAS_CACHE = 1

ESPERA_CONFIRMADO_S = 5.0
TENTATIVAS_CONFIRMADO = 2
INTERVALO_TENTATIVAS_S = 1.0


# Estados da conexao. Sao o que a marca da barra de titulo mostra: verde
# conectada, cinza sem conexao, com o detalhe na dica dela.
PARADO = "parado"
PROCURANDO = "procurando"
CONECTADA = "conectada"
SEM_LAMPADA = "sem_lampada"
ERRO = "erro"


class Lampada:
    """
    A lampada, vista pela janela.

    Todos os metodos publicos podem ser chamados da thread da interface: eles
    so enfileiram e voltam na hora.
    """

    def __init__(
        self,
        config: Config,
        ao_estado: Callable[[dict], None] | None = None,
        ao_conexao: Callable[[str, str], None] | None = None,
        agendar: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.config = config

        self._ao_estado = ao_estado
        self._ao_conexao = ao_conexao
        self._agendar = agendar

        self._fila: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._parar = threading.Event()
        self._acordar = threading.Event()   # "procurar agora": corta a espera entre tentativas

        self._aparelho = None          # o objeto do tinytuya
        self._conexao = PARADO
        self._ultimo_dps: dict = {}

    # -- o que a janela chama -------------------------------------------------

    def iniciar(self) -> None:
        """Sobe a thread. Conectar acontece nela, nao aqui."""
        if self._thread is not None:
            return

        self._parar.clear()
        self._thread = threading.Thread(
            target=self._rodar, name="lampada", daemon=True
        )
        self._thread.start()

    def reconectar(self) -> None:
        """
        Sobe a thread de novo depois que a lampada foi configurada.

        A thread SAI sozinha quando nao ha chave nenhuma (ver `_rodar`):
        ficar acordando a cada poucos segundos para reconferir um arquivo que
        so muda por acao da pessoa seria trabalho para nada. Quem sabe que a
        configuracao mudou e a janela, e e ela que chama isto.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = None
        self.iniciar()

    def encerrar(self, espera_s: float = 2.0) -> None:
        """Fecha a conexao e derruba a thread."""
        self._parar.set()
        self._acordar.set()
        self._fila.put(("sair", None))

        if self._thread is not None:
            self._thread.join(timeout=espera_s)
            self._thread = None

    def ligar(self, ligado: bool) -> None:
        self._fila.put(("ligar", bool(ligado)))

    def alternar(self) -> None:
        """Inverte o estado conhecido. E o que o botao grande faz."""
        self._fila.put(("alternar", None))

    def temperatura(self, valor: int) -> None:
        valor = max(TEMP_MIN, min(TEMP_MAX, int(valor)))
        self._fila.put(("temperatura", valor))

    def procurar_agora(self) -> None:
        """Corta a espera entre duas tentativas de conexao."""
        self._acordar.set()
        self.reconectar()

    def atualizar(self) -> None:
        """Pede uma releitura do estado agora, sem esperar a sondagem."""
        self._fila.put(("atualizar", None))

    @property
    def conexao(self) -> str:
        return self._conexao

    @property
    def estado(self) -> dict:
        """O ultimo estado conhecido. Copia, para ninguem mexer no de dentro."""
        return dict(self._ultimo_dps)

    # -- avisos ---------------------------------------------------------------

    def _avisar(self, funcao: Callable[[], None]) -> None:
        """Entrega um aviso na thread certa."""
        if self._agendar is not None:
            try:
                self._agendar(funcao)
            except Exception as erro:
                # A janela pode ter sido destruida entre o aviso e a entrega.
                log.debug("aviso descartado: %s", erro)
            return
        funcao()

    def _mudou_conexao(self, estado: str, detalhe: str = "") -> None:
        if estado == self._conexao and not detalhe:
            return
        self._conexao = estado
        if self._ao_conexao is not None:
            self._avisar(lambda: self._ao_conexao(estado, detalhe))

    def _mudou_estado(self, dps: dict) -> None:
        self._ultimo_dps = dict(dps)
        if self._ao_estado is not None:
            copia = dict(dps)
            self._avisar(lambda: self._ao_estado(copia))

    # -- a thread -------------------------------------------------------------

    def _rodar(self) -> None:
        if not self.config.completo:
            self._mudou_conexao(
                SEM_LAMPADA, "nenhuma lampada configurada"
            )
            return

        proxima_sondagem = 0.0

        while not self._parar.is_set():
            if self._aparelho is None:
                if not self._conectar():
                    # Nao adianta insistir sem parar; espera, mas acorda na
                    # hora se chegar pedido de saida ou de "procurar agora".
                    self._dormir(ESPERA_RECONEXAO_S)
                    continue
                proxima_sondagem = time.monotonic() + INTERVALO_SONDAGEM_S

            try:
                pedidos = [self._fila.get_nowait()]
            except queue.Empty:
                if time.monotonic() >= proxima_sondagem:
                    self._sondar()
                    proxima_sondagem = time.monotonic() + INTERVALO_SONDAGEM_S
                elif self._escutar():
                    proxima_sondagem = time.monotonic() + INTERVALO_SONDAGEM_S
                continue

            # Cliques seguidos viram UM comando de temperatura: so o ultimo
            # valor importa (padrao "estabilidade" da casa).
            while True:
                try:
                    pedidos.append(self._fila.get_nowait())
                except queue.Empty:
                    break
            ultima_temp = max((i for i, (a, _v) in enumerate(pedidos) if a == "temperatura"), default=-1)

            sair = False
            for i, (acao, valor) in enumerate(pedidos):
                if acao == "sair":
                    sair = True
                    break
                if acao == "temperatura" and i != ultima_temp:
                    continue
                self._executar(acao, valor)
            if sair:
                break
            # Depois de agir, a proxima sondagem pode esperar: o comando ja
            # devolveu o estado novo.
            proxima_sondagem = time.monotonic() + INTERVALO_SONDAGEM_S

        self._desconectar()
        self._mudou_conexao(PARADO)

    def _escutar(self) -> bool:
        """Espera ate ESCUTA_S por um aviso da lampada. True = chegou estado.

        Sem socket aberto (o tinytuya reabre no proximo comando), so espera."""
        sock = getattr(self._aparelho, "socket", None)
        if sock is None:
            time.sleep(ESCUTA_S)
            return False
        try:
            prontos = select.select([sock], [], [], ESCUTA_S)[0]
        except (OSError, ValueError):
            self._perdeu_a_conexao("o socket fechou")
            return False
        if not prontos:
            return False
        try:
            resposta = self._aparelho.receive()
        except Exception as erro:
            self._perdeu_a_conexao(str(erro))
            return False
        if isinstance(resposta, dict) and "Error" in resposta:
            self._perdeu_a_conexao("a lampada fechou a conexao")
            return False
        dps = resposta.get("dps") if isinstance(resposta, dict) else None
        if not isinstance(dps, dict) or not dps:
            return False                         # eco vazio / batida: nada a mostrar
        # o aviso traz so o que mudou; o resto continua valendo
        juntos = dict(self._ultimo_dps)
        juntos.update(dps)
        if juntos != self._ultimo_dps:
            log.info("mudou por fora: %s", {k: v for k, v in dps.items() if k in (DP_LIGADO, DP_MODO, DP_TEMPERATURA)})
            self._mudou_estado(juntos)
        return True

    def _dormir(self, segundos: float) -> None:
        """Espera, mas acorda cedo se pedirem para sair ou procurar agora."""
        self._acordar.wait(timeout=segundos)
        self._acordar.clear()

    # -- conexao --------------------------------------------------------------

    def _conectar(self) -> bool:
        """
        Abre a conexao. Primeiro no IP guardado, depois procurando na rede.

        A ordem importa: o IP guardado acerta na maioria das vezes e custa
        milissegundos, enquanto a varredura espera a lampada se anunciar
        sozinha e pode levar segundos.
        """
        import tinytuya

        if self.config.ip:
            self._mudou_conexao(PROCURANDO, "tentando %s" % self.config.ip)
            if self._abrir(tinytuya, self.config.ip, rapido=True):
                return True
            log.info("a lampada nao respondeu em %s; procurando na rede",
                     self.config.ip)

        self._mudou_conexao(PROCURANDO, "procurando na rede")

        try:
            achado = tinytuya.find_device(dev_id=self.config.id)
        except Exception as erro:
            log.warning("a busca na rede falhou: %s", erro)
            achado = {}

        ip = (achado or {}).get("ip")
        if not ip:
            self._mudou_conexao(ERRO, "lampada nao encontrada na rede")
            return False

        if self._abrir(tinytuya, ip):
            self.config.lembrar_ip(ip)
            return True

        self._mudou_conexao(ERRO, "nao consegui falar com a lampada")
        return False

    def _abrir(self, tinytuya, ip: str, rapido: bool = False) -> bool:
        """
        Monta o objeto, aponta para `ip` e confirma com uma leitura.

        `rapido` escolhe o perfil de espera -- ver o bloco de constantes no
        topo. Sem ele, uma tentativa num IP morto segura a thread por quase um
        minuto e a busca na rede nunca chega a rodar.
        """
        if rapido:
            espera, tentativas, intervalo = ESPERA_CACHE_S, TENTATIVAS_CACHE, 0
        else:
            espera = ESPERA_CONFIRMADO_S
            tentativas = TENTATIVAS_CONFIRMADO
            intervalo = INTERVALO_TENTATIVAS_S

        try:
            aparelho = tinytuya.BulbDevice(
                self.config.id, ip, self.config.chave,
                connection_timeout=espera,
                connection_retry_limit=tentativas,
                connection_retry_delay=intervalo,
            )
            aparelho.set_version(3.5)
            aparelho.set_socketPersistent(True)

            resposta = aparelho.status()
            dps = self._dps_de(resposta)
            if dps is None:
                self._fechar(aparelho)
                return False

            self._aparelho = aparelho
            self._mudou_conexao(CONECTADA, ip)
            self._mudou_estado(dps)
            return True
        except Exception as erro:
            log.debug("nao consegui abrir em %s: %s", ip, erro)
            return False

    def _desconectar(self) -> None:
        if self._aparelho is not None:
            self._fechar(self._aparelho)
            self._aparelho = None

    @staticmethod
    def _fechar(aparelho) -> None:
        try:
            aparelho.close()
        except Exception:
            pass

    def _perdeu_a_conexao(self, motivo: str) -> None:
        """Descarta a conexao para o proximo ciclo refazer."""
        log.info("conexao perdida: %s", motivo)
        self._desconectar()
        self._mudou_conexao(ERRO, motivo)

    # -- comandos -------------------------------------------------------------

    @staticmethod
    def _dps_de(resposta) -> dict | None:
        """
        Tira os DPs da resposta do tinytuya, ou None se ela nao presta.

        A biblioteca devolve tres formatos: o bom, `{'dps': {...}}`; o de
        falha, `{'Error': ...}`; e, quando o socket morre, qualquer outra
        coisa. Todos os tres passam por aqui.
        """
        if not isinstance(resposta, dict):
            return None
        if "Error" in resposta:
            log.debug("a lampada respondeu com erro: %s", resposta.get("Error"))
            return None
        dps = resposta.get("dps")
        return dps if isinstance(dps, dict) else None

    def _sondar(self, forcar: bool = False) -> None:
        """
        Le o estado. E o unico lugar que descobre mudanca feita por fora.

        `forcar` faz avisar mesmo quando nada mudou. Quem PEDIU a releitura
        merece resposta -- inclusive "continua igual" -- enquanto a sondagem
        automatica, que roda sozinha a cada `INTERVALO_SONDAGEM_S`, so fala
        quando ha novidade. Sem essa diferenca, um pedido de atualizar cai no
        vazio e quem esperava resposta espera para sempre.
        """
        if self._aparelho is None:
            return

        try:
            dps = self._dps_de(self._aparelho.status())
        except Exception as erro:
            self._perdeu_a_conexao(str(erro))
            return

        if dps is None:
            self._perdeu_a_conexao("a lampada parou de responder")
            return

        if forcar or dps != self._ultimo_dps:
            self._mudou_estado(dps)

    def _executar(self, acao: str, valor) -> None:
        if self._aparelho is None:
            return

        if acao == "atualizar":
            self._sondar(forcar=True)
            return

        if acao == "alternar":
            acao, valor = "ligar", not bool(self._ultimo_dps.get(DP_LIGADO))

        try:
            if acao == "ligar":
                resposta = self._aparelho.set_value(DP_LIGADO, bool(valor))
            elif acao == "temperatura":
                # Em modo colour, scene ou music a lampada ignora a
                # temperatura. Acender, forcar `white` e mandar o valor vao
                # juntos no mesmo envio: separados, seriam tres idas e voltas
                # e a lampada piscaria entre elas.
                resposta = self._aparelho.set_multiple_values({
                    DP_LIGADO: True,
                    DP_MODO: "white",
                    DP_TEMPERATURA: int(valor),
                })
            else:
                log.warning("acao desconhecida: %s", acao)
                return
        except Exception as erro:
            self._perdeu_a_conexao(str(erro))
            return

        dps = self._dps_de(resposta)
        if dps is not None:
            # A lampada costuma devolver so o que mudou; o resto continua
            # valendo.
            juntos = dict(self._ultimo_dps)
            juntos.update(dps)
            self._mudou_estado(juntos)
        else:
            # Comando aceito sem eco util: le para saber o que aconteceu de
            # verdade, em vez de supor que funcionou.
            self._sondar()
