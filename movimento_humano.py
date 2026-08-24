"""Movimento humano de mouse para clicar no checkbox do hCaptcha.

Contém:
  - replay das gravações REAIS do usuário (Captcha/movimentos/*.json);
  - fallback sintético: curva de Bézier com jitter e velocidade variável.

Tudo usa PyAutoGUI (mouse do sistema operacional, indetectável pelo site).
"""

import glob
import json
import os
import random
import time

PASTA_MOVIMENTOS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "movimentos"
)


def pyautogui_ok():
    """Importa e configura o PyAutoGUI (mesmo fix X11 que o SeleniumBase usa)."""
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.01
    if os.name == "posix" and "DISPLAY" in os.environ and hasattr(
        pyautogui, "_pyautogui_x11"
    ):
        import Xlib.display
        pyautogui._pyautogui_x11._display = Xlib.display.Display(
            os.environ["DISPLAY"]
        )
    return pyautogui


def _cubic_bezier(p0, p1, p2, p3, t):
    """Ponto da curva de Bézier cúbica no parâmetro t (0..1)."""
    u = 1 - t
    return (
        u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0],
        u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1],
    )


def _ease(t):
    """Ease-out: começa rápido e desacelera no final (como a mão humana)."""
    return 1 - (1 - t) ** 3


def mover_mouse_humano(pyautogui, x, y, rng):
    """Move o cursor até (x, y) com trajetória curva + velocidade variável.

    Simula uma mão real: parte de um ponto aleatório, acelera no meio do
    caminho, desacelera na chegada, hesita e termina com micro-correção."""
    largura, altura = pyautogui.size()

    # escolhe a direção de chegada: geralmente de cima/esquerda, às vezes outro lado
    direcao_x = rng.uniform(-1, 1)
    direcao_y = rng.uniform(-1, 1)
    distancia = rng.uniform(200, 420)
    inicio = (
        max(10, min(largura - 10, x + direcao_x * distancia)),
        max(10, min(altura - 10, y + direcao_y * distancia)),
    )

    # pontos de controle da Bézier (curvam o caminho)
    off1 = rng.uniform(60, 220)
    off2 = rng.uniform(-220, -60)
    p1 = (x + direcao_x * off1, y + direcao_y * off2)
    p2 = (x + direcao_x * off2 * 0.7, y + direcao_y * off1 * 0.7)

    # alvo real: desvio pequeno no centro do checkbox (não precisa ser exato)
    alvo = (x + rng.uniform(-4, 4), y + rng.uniform(-4, 4))

    passos = rng.randint(38, 62)  # ~60Hz de amostragem de mouse
    anterior = None
    for i in range(passos + 1):
        t = _ease(i / passos)
        px, py = _cubic_bezier(inicio, p1, p2, alvo, t)
        px += rng.uniform(-2.5, 2.5)   # jitter natural
        py += rng.uniform(-2.5, 2.5)
        pyautogui.moveTo(px, py, _pause=0)
        if anterior is not None:
            # sleep entre 5 e 18ms (input real de mouse)
            time.sleep(rng.uniform(0.005, 0.018))
        anterior = (px, py)

        # hesitação ocasional no meio do trajeto (humano "decidindo")
        if i % 13 == 6 and rng.random() < 0.4:
            time.sleep(rng.uniform(0.04, 0.12))

    # micro-correção final: mão assenta no alvo
    time.sleep(rng.uniform(0.06, 0.2))
    pyautogui.moveTo(alvo[0] + rng.uniform(-1.5, 1.5), alvo[1] + rng.uniform(-1.5, 1.5), _pause=0)

    # hesitação antes do clique (humano confere antes de clicar)
    time.sleep(rng.uniform(0.1, 0.35))


def clicar_humano(pyautogui, x, y, rng):
    """Clique com tempo de pressionamento natural (não "clique" instantâneo)."""
    pyautogui.mouseDown(x=x, y=y)
    time.sleep(rng.uniform(0.05, 0.14))  # pressão do dedo no botão
    pyautogui.mouseUp(x=x, y=y)


def escolher_gravacao(rng):
    """Sorteia um arquivo de movimento gravado (ou None se não houver)."""
    try:
        arquivos = sorted(glob.glob(os.path.join(PASTA_MOVIMENTOS, "*.json")))
    except Exception:
        arquivos = []
    if not arquivos:
        return None
    return rng.choice(arquivos)


def replay_gravado(pyautogui, x, y, rng, arquivo):
    """Reproduz o movimento que o usuário gravou, com pequenas variações.

    As amostras são relativas ao ponto do clique gravado e são reescaladas
    para a posição atual do checkbox (e para o tamanho atual da tela).
    """
    with open(arquivo, "r") as f:
        dados = json.load(f)

    largura, altura = pyautogui.size()
    tela = dados.get("tela", {})
    sx = (largura / tela["largura"]) if tela.get("largura") else 1.0
    sy = (altura / tela["altura"]) if tela.get("altura") else 1.0
    cx, cy = dados["clique"]["x"], dados["clique"]["y"]
    duracao_clique = dados["clique"].get("duracao", 100) / 1000.0
    fator_tempo = rng.uniform(0.92, 1.12)

    t_anterior = None
    for t_ms, px, py_ in dados["amostras"]:
        novo_x = x + (px - cx) * sx + rng.uniform(-1.5, 1.5)
        novo_y = y + (py_ - cy) * sy + rng.uniform(-1.5, 1.5)
        novo_x = max(5, min(largura - 5, novo_x))
        novo_y = max(5, min(altura - 5, novo_y))

        if t_anterior is not None:
            # espera só o tempo restante (o próprio moveTo já leva ~2ms)
            time.sleep(max(0.0, (t_ms / 1000.0 - t_anterior) * fator_tempo - 0.002))
        pyautogui.moveTo(novo_x, novo_y, _pause=0)
        t_anterior = t_ms / 1000.0

    # hesitação natural antes do clique + duração de pressionamento gravada
    time.sleep(rng.uniform(0.06, 0.2))
    pyautogui.mouseDown(x=x, y=y)
    time.sleep(max(0.03, duracao_clique * fator_tempo))
    pyautogui.mouseUp(x=x, y=y)


def clicar_checkbox_humano(pyautogui, x, y):
    """Clica em (x, y) com movimento humano.

    Usa a trajetória gravada quando houver (Captcha/movimentos/); caso
    contrário cai no movimento sintético (curva de Bézier humana)."""
    rng = random.Random()
    gravacao = escolher_gravacao(rng)
    if gravacao:
        print("[+] Reproduzindo movimento gravado: %s" % os.path.basename(gravacao))
        replay_gravado(pyautogui, x, y, rng, gravacao)
    else:
        print("[+] Sem gravações em Captcha/movimentos/ — usando curva sintética.")
        mover_mouse_humano(pyautogui, x, y, rng)
        clicar_humano(pyautogui, x, y, rng)


# =====================================================================
# Versões para PLAYWRIGHT (page.mouse — eventos de input reais via CDP).
# As coordenadas são relativas ao VIEWPORT da página: o alvo (x, y) é o
# centro do checkbox nesse espaço. Dispensa conversão p/ tela e funciona
# mesmo sem a janela ter foco.
# =====================================================================

def _viewport(page):
    return page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")


def _escala_playwright(page, dados):
    """Escala entre a tela gravada e o viewport atual (trajetória relativa)."""
    vp = _viewport(page)
    tela = dados.get("tela", {})
    sx = (vp["w"] / tela["largura"]) if tela.get("largura") else 1.0
    sy = (vp["h"] / tela["altura"]) if tela.get("altura") else 1.0
    return sx, sy


def replay_gravado_playwright(page, x, y, rng, arquivo):
    """Reproduz o movimento gravado usando o mouse do Playwright."""
    with open(arquivo, "r") as f:
        dados = json.load(f)

    sx, sy = _escala_playwright(page, dados)
    cx, cy = dados["clique"]["x"], dados["clique"]["y"]
    duracao_clique = dados["clique"].get("duracao", 100) / 1000.0
    fator_tempo = rng.uniform(0.92, 1.12)

    t_anterior = None
    for t_ms, px, py_ in dados["amostras"]:
        novo_x = x + (px - cx) * sx + rng.uniform(-1.5, 1.5)
        novo_y = y + (py_ - cy) * sy + rng.uniform(-1.5, 1.5)
        if t_anterior is not None:
            # espera só o tempo restante (o próprio move já leva ~1ms)
            time.sleep(max(0.0, (t_ms / 1000.0 - t_anterior) * fator_tempo - 0.002))
        page.mouse.move(novo_x, novo_y)
        t_anterior = t_ms / 1000.0

    # assenta no alvo, hesita e clica com a duração gravada
    time.sleep(rng.uniform(0.06, 0.2))
    page.mouse.move(x + rng.uniform(-1.5, 1.5), y + rng.uniform(-1.5, 1.5))
    time.sleep(rng.uniform(0.08, 0.25))
    page.mouse.down()
    time.sleep(max(0.03, duracao_clique * fator_tempo))
    page.mouse.up()


def mover_mouse_humano_playwright(page, x, y, rng):
    """Curva sintética (Bézier humana) via mouse do Playwright."""
    vp = _viewport(page)
    largura, altura = vp["w"], vp["h"]

    direcao_x = rng.uniform(-1, 1)
    direcao_y = rng.uniform(-1, 1)
    distancia = rng.uniform(150, 350)
    inicio = (
        max(10, min(largura - 10, x + direcao_x * distancia)),
        max(10, min(altura - 10, y + direcao_y * distancia)),
    )

    off1 = rng.uniform(50, 180)
    off2 = rng.uniform(-180, -50)
    p1 = (x + direcao_x * off1, y + direcao_y * off2)
    p2 = (x + direcao_x * off2 * 0.7, y + direcao_y * off1 * 0.7)

    alvo = (x + rng.uniform(-4, 4), y + rng.uniform(-4, 4))
    passos = rng.randint(38, 62)

    page.mouse.move(inicio[0], inicio[1])
    time.sleep(rng.uniform(0.1, 0.3))
    anterior = None
    for i in range(passos + 1):
        t = _ease(i / passos)
        px, py = _cubic_bezier(inicio, p1, p2, alvo, t)
        px += rng.uniform(-2.5, 2.5)
        py += rng.uniform(-2.5, 2.5)
        page.mouse.move(px, py)
        if anterior is not None:
            time.sleep(rng.uniform(0.005, 0.018))
        anterior = (px, py)
        if i % 13 == 6 and rng.random() < 0.4:
            time.sleep(rng.uniform(0.04, 0.12))

    time.sleep(rng.uniform(0.06, 0.2))
    page.mouse.move(alvo[0] + rng.uniform(-1.5, 1.5), alvo[1] + rng.uniform(-1.5, 1.5))
    time.sleep(rng.uniform(0.1, 0.35))


def clicar_humano_playwright(page, x, y, rng):
    """Clique com tempo de pressionamento natural via mouse do Playwright."""
    page.mouse.move(x, y)
    time.sleep(rng.uniform(0.05, 0.15))
    page.mouse.down()
    time.sleep(rng.uniform(0.05, 0.14))
    page.mouse.up()


def clicar_checkbox_humano_playwright(page, x, y):
    """Clica em (x, y) com movimento humano via mouse do Playwright.

    Usa a trajetória gravada quando houver (Captcha/movimentos/); caso
    contrário cai no movimento sintético (curva de Bézier humana)."""
    rng = random.Random()
    gravacao = escolher_gravacao(rng)
    if gravacao:
        print("[+] Reproduzindo movimento gravado: %s" % os.path.basename(gravacao))
        replay_gravado_playwright(page, x, y, rng, gravacao)
    else:
        print("[+] Sem gravações em Captcha/movimentos/ — usando curva sintética.")
        mover_mouse_humano_playwright(page, x, y, rng)
        clicar_humano_playwright(page, x, y, rng)
