"""
Converte o valor de temperatura da lampada (0 a 1000) na cor que a tela
mostra.

O QUE ESTE MODULO NAO SABE
--------------------------
Qual e a faixa real desta lampada. O esquema da Tuya diz "0 a 1000" e nao diz
a quantos kelvin isso corresponde. 2700K a 6500K e o intervalo padrao de
lampada branca ajustavel, e e o que esta aqui.

Se um dia der para medir, e so mexer em `KELVIN_MIN` e `KELVIN_MAX` -- o
resto se ajusta. O sinal de que o palpite esta errado seria as seis pastilhas
saindo parecidas demais entre si na tela e distintas na parede, ou o
contrario.

DE ONDE VEM A COR
-----------------
Aproximacao de Kang et al. (2002) para a curva do corpo negro, seguida de
conversao para sRGB. E a mesma que o AmbiSync usa, so que sem numpy: aqui sao
seis cores calculadas de vez em quando, nao um quadro inteiro por segundo.

NORMALIZADA PELO MAIOR CANAL, de proposito. A cor fisicamente correta de 2700K
e escura; na tela isso pareceria uma lampada com pouco brilho, e brilho e
outra coisa. Dividindo pelo maior canal, o que sobra e so o MATIZ -- que e o
que a pastilha precisa comunicar.
"""

from __future__ import annotations

# A faixa que se supoe para os valores 0 e 1000 do DP 23.
KELVIN_MIN = 2700   # valor 0    -- amarelado, confirmado na parede
KELVIN_MAX = 6500   # valor 1000 -- azulado, confirmado na parede

VALOR_MIN = 0
VALOR_MAX = 1000

# Os seis valores que viram botao na janela. Seis porque cabem na largura sem
# apertar e ainda cobrem a faixa inteira de ponta a ponta; com mais, os tons
# vizinhos ficariam indistinguiveis e a escolha viraria adivinhacao.
ESCOLHAS = (0, 200, 400, 600, 800, 1000)


def para_kelvin(valor: int) -> int:
    """0..1000 -> kelvin. Serve para a legenda embaixo do circulo."""
    valor = max(VALOR_MIN, min(VALOR_MAX, int(valor)))
    fracao = (valor - VALOR_MIN) / (VALOR_MAX - VALOR_MIN)
    return int(round(KELVIN_MIN + (KELVIN_MAX - KELVIN_MIN) * fracao))


def _cromaticidade(kelvin: float) -> tuple[float, float]:
    """Ponto (x, y) da curva do corpo negro, aproximacao de Kang."""
    t = max(1667.0, min(25000.0, float(kelvin)))

    if t <= 4000:
        x = (-0.2661239e9 / t ** 3 - 0.2343589e6 / t ** 2
             + 0.8776956e3 / t + 0.179910)
    else:
        x = (-3.0258469e9 / t ** 3 + 2.1070379e6 / t ** 2
             + 0.2226347e3 / t + 0.240390)

    if t <= 2222:
        y = (-1.1063814 * x ** 3 - 1.34811020 * x ** 2
             + 2.18555832 * x - 0.20219683)
    elif t <= 4000:
        y = (-0.9549476 * x ** 3 - 1.37418593 * x ** 2
             + 2.09137015 * x - 0.16748867)
    else:
        y = (3.0817580 * x ** 3 - 5.87338670 * x ** 2
             + 3.75112997 * x - 0.37001483)

    return x, y


def canais_de_kelvin(kelvin: float) -> tuple[int, int, int]:
    """Kelvin -> (r, g, b) de 0 a 255, normalizado pelo maior canal."""
    x, y = _cromaticidade(kelvin)

    # xyY -> XYZ, com Y fixo em 1: so o matiz interessa.
    grande_y = 1.0
    grande_x = x * grande_y / y
    grande_z = (1 - x - y) * grande_y / y

    r = 3.2406 * grande_x - 1.5372 * grande_y - 0.4986 * grande_z
    g = -0.9689 * grande_x + 1.8758 * grande_y + 0.0415 * grande_z
    b = 0.0557 * grande_x - 0.2040 * grande_y + 1.0570 * grande_z

    maior = max(r, g, b, 1e-9)

    saida = []
    for canal in (r / maior, g / maior, b / maior):
        canal = max(0.0, min(1.0, canal))
        # Gama do sRGB: sem isso a cor sai lavada demais nos tons quentes.
        if canal <= 0.0031308:
            canal *= 12.92
        else:
            canal = 1.055 * canal ** (1 / 2.4) - 0.055
        saida.append(int(round(canal * 255)))

    return saida[0], saida[1], saida[2]


def canais(valor: int) -> tuple[int, int, int]:
    """0..1000 -> (r, g, b)."""
    return canais_de_kelvin(para_kelvin(valor))


def cor(valor: int) -> str:
    """0..1000 -> '#rrggbb', pronto para o Tk."""
    r, g, b = canais(valor)
    return "#%02x%02x%02x" % (r, g, b)
