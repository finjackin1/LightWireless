# LightWireless

Controle de lâmpada inteligente Elgin (Wi-Fi, base Tuya) para Windows.
Uma janela pequena de tamanho fixo, um clique para acender, seis temperaturas
de branco, timer, agenda por horário e atalhos de teclado que funcionam com o
programa em segundo plano.

Fala com a lâmpada **direto na rede local** — sem nuvem e sem depender da
internet. A conta do app Tuya Smart só é usada uma vez, para obter a chave
do aparelho.

> Projeto independente, sem ligação com a Elgin ou a Tuya. Use por sua conta.

---

## O que ele faz

- **Liga e apaga** com um clique (teclas 1 e 0 na janela).
- **Seis temperaturas de branco**, de 2700 K a 6500 K, cada botão com a
  lâmpada pintada na própria cor (teclas + e −).
- **Timer**: apaga sozinha depois de X minutos.
- **Agenda**: ligar, apagar ou escolher o tom em horários e dias da semana
  (com o PC ligado).
- **Atalhos globais de teclado** (nascem vazios; você escolhe): ligar/desligar,
  ligar, desligar, mais quente, mais fria, mostrar/esconder a janela,
  desconectar/reconectar. Funcionam com qualquer programa em foco.
- **Ícone na bandeja**: um clique alterna a luz, dois cliques abrem a janela;
  o ícone fica na cor da luz.
- Em Opções: **iniciar com o Windows** (e "iniciar mais rápido", junto com o
  login), **iniciar minimizado**, **ao conectar** e **ao sair** (cada um:
  nada, ligar, apagar ou um tom).
- **Acompanha a lâmpada na hora**: mexeu pelo app do celular, a janela e a
  bandeja mudam junto.
- Diz na tela o que fazer quando não acha a lâmpada.

## O que ele não faz (ainda)

Brilho, cores RGB, cenas, sincronia com a tela ou com música. A versão 1 é
liga/desliga e temperatura de branco, de propósito.

---

## Instalação (versão pronta, sem Python)

1. Baixe o zip da versão mais recente e extraia em qualquer pasta.
2. Dê um duplo clique em `LightWireless.exe`.
3. Na primeira abertura clique em **Configurar**: ele **procura a lâmpada na
   rede** e mostra o que achou. Vai faltar só a chave.

### A chave

Cada lâmpada Tuya tem uma chave que autoriza conversar com ela na rede local.
Ela **não** viaja pela rede: só existe na nuvem do fabricante, e vem de lá
uma vez. Não há caminho local — para aparelho Wi-Fi Tuya, não existe.

**Jeito mais fácil: "Entrar com conta Tuya"** (a lâmpada cadastrada no app
**Tuya Smart** ou **Smart Life**; sem conta de desenvolvedor, sem senha):

1. No app: **Eu → ⚙ (Configurações) → Conta e segurança → User Code**.
2. No programa: **Configurar** (ou Opções → **Atualizar chave**) →
   **Entrar com conta Tuya**, digite o User Code e clique em **Gerar QR**.
3. No app, toque no leitor de QR (canto de cima da tela inicial), aponte para
   o QR e toque em **Confirmar login**.

O programa lê a lista de aparelhos da conta, acha qual deles é a lâmpada da
sua rede, grava o id e a chave e conecta. Senha, login (tokens) e User Code
**não** são guardados; a sessão é encerrada logo em seguida.

> Esse login usa o mesmo caminho oficial da Tuya que o Home Assistant usa
> (biblioteca `tuya-device-sharing-sdk` e o registro público do Home
> Assistant). A Tuya chama esse registro de **provisório**: se um dia ele
> deixar de funcionar, os outros caminhos continuam aqui.

**Outros caminhos:** **"Colar a chave"** aceita a chave local (16 caracteres)
direto; **"Usar o devices.json"** lê o arquivo do assistente do tinytuya; e
**"Portal de desenvolvedor"** busca com o Access ID/Secret de um projeto em
iot.tuya.com (o acesso gratuito de lá expira e precisa ser renovado).

Depois disso o programa **não fala mais com nuvem nenhuma**: só com a lâmpada,
dentro da sua rede.

> **Resetou a lâmpada ou trocou de Wi-Fi?** O reset gera uma chave nova:
> Opções → **Atualizar chave**. O `config.json` anterior fica salvo como
> `config.json.bak`.

---

## Instalação a partir do código-fonte

Requer **Python 3.11 ou mais novo** no Windows.

1. Baixe ou clone este repositório.
2. Obtenha a chave da sua lâmpada com o assistente do tinytuya:

   ```
   python -m pip install tinytuya
   python -m tinytuya wizard
   ```

   Ele pede uma conta gratuita de desenvolvedor Tuya e gera um `devices.json`.
3. Copie esse `devices.json` para a pasta do LightWireless. Na primeira
   execução o programa converte o que precisa para o `config.json` dele e você
   pode apagar o original.
4. Instale as dependências e abra:

   ```
   python -m pip install -r requirements.txt
   pythonw LightWireless.pyw
   ```

### Dependências

| Biblioteca | Para quê | Licença |
|---|---|---|
| [tinytuya](https://github.com/jasonacox/tinytuya) | Falar com a lâmpada. É a única que implementa o protocolo Tuya 3.5, que é o desta lâmpada | MIT |
| [Pillow](https://python-pillow.org/) | Desenhar a marca, os ícones e os botões | MIT-CMU |
| [pystray](https://github.com/moses-palmer/pystray) | Ícone na bandeja. Opcional de verdade: sem ela o programa abre e funciona | LGPLv3 |
| [tuya-device-sharing-sdk](https://github.com/tuya/tuya-device-sharing-sdk) | "Entrar com conta Tuya" (User Code + QR), só para pegar a chave | MIT |
| [cryptography](https://github.com/pyca/cryptography) | AES-GCM (o SDK acima usa sem declarar) | Apache-2.0 / BSD |
| [segno](https://github.com/heuer/segno) | Desenhar o QR do login | BSD-3-Clause |

Os atalhos globais **não** usam biblioteca de teclado. Quem compara a
combinação é o próprio Windows, pelo mecanismo oficial de atalho — o programa
não vê nada do que você digita, não instala gancho de teclado e não é barrado
por antivírus.

---

## Compilando do código-fonte

O executável distribuído é gerado a partir deste mesmo código:

```
git clone https://github.com/finjackin1/LightWireless
cd LightWireless
python -m pip install -r requirements.txt pyinstaller
python ferramentas\empacotar.py
```

O resultado sai em `dist\LightWireless\` (pasta portable) e
`dist\LightWireless-<versão>-windows.zip`. O script roda um autoteste do .exe
e confere que nenhuma chave entrou no zip.

---

## Privacidade

O `config.json` (na pasta do programa) guarda a chave local da sua lâmpada, o
último endereço dela na rede e suas opções. Nada é instalado; o registro do
Windows só é tocado se você ligar "Iniciar com o Windows". Ele **nunca** é versionado (está no `.gitignore`) e nunca sai da sua
máquina. O programa só fala com a internet quando **você** pede para pegar a
chave (conta Tuya ou portal); fora disso, toda a conversa é com a lâmpada,
dentro da sua rede.

---

## Como foi feito

O LightWireless foi desenvolvido com apoio de inteligência artificial: o código foi feito em conversa
com o **Claude**, da Anthropic (no Claude Code). As decisões de projeto, o desenho da interface, os
testes com a lâmpada de verdade e o rumo de cada funcionalidade são de
[finjackin](https://github.com/finjackin1).

---

## Licença

GPLv3 — veja [LICENSE](LICENSE). Em resumo: use, estude, modifique e
redistribua à vontade, desde que o resultado continue livre e com o
código-fonte disponível. As licenças das bibliotecas usadas estão em
[THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt).
