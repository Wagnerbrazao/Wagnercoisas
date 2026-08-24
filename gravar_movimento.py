"""
Grava SEUS movimentos reais de mouse clicando no checkbox do hCaptcha.
Depois, o bot reproduz exatamente esse movimento (com pequenas variações).

NÃO precisa de teclado nem de terminal interativo: é só usar o mouse.

Leitura da posição do mouse (automático):
  1. X11 (query_pointer) — funciona SEM sudo, é o caminho normal;
  2. evdev (/dev/input) — usado apenas se o X11 falhar (ex.: Wayland puro).
     Nesse caso exige permissão:  sudo usermod -aG input $USER
     (ou rode com sudo). A escala pode ser ajustada com ESCALA abaixo.

Como usar:
  1. Abra a página da Receita com o hCaptcha em uma janela do navegador.
  2. Rode:  venv/bin/python Captcha/gravar_movimento.py
  3. Movimente o mouse NATURALMENTE até o checkbox e clique UMA vez.
     Repita quantas vezes quiser — cada clique vira um arquivo em
     Captcha/movimentos/ (o bot sorteia um a cada execução).
  4. Ctrl+C no terminal para encerrar.
"""

import json
import os
import select
import sys
import time
from datetime import datetime

PASTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "movimentos")
JANELA = 6.0              # segundos de histórico mantidos antes do clique
POS_CLIQUE = 0.2          # segundos extras gravados depois de soltar o botão
MOVIMENTO_MINIMO = 25     # distância mínima (px) para a gravação valer
DURACAO_MINIMA = 0.15     # duração mínima (s) para a gravação valer
TAXA = 0.008              # ~120 amostras/segundo (X11)
ESCALA = 3.0              # fator p/ converter deltas do evdev em pixels (Wayland)


def tamanho_tela():
    """Descobre a resolução da tela (via Xlib/XWayland); fallback 1920x1080."""
    try:
        import Xlib.display
        g = Xlib.display.Display().screen().root.get_geometry()
        return g.width, g.height
    except Exception:
        return 1920, 1080


def salvar(dados):
    os.makedirs(PASTA, exist_ok=True)
    nome = "gravacao_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    caminho = os.path.join(PASTA, nome)
    with open(caminho, "w") as f:
        json.dump(dados, f)
    return caminho


class X11Mouse:
    """Lê a posição ABSOLUTA do cursor via X11 — sem precisar de sudo."""

    def __init__(self):
        import Xlib.display
        self.display = Xlib.display.Display()
        self.raiz = self.display.screen().root

    def posicao(self):
        p = self.raiz.query_pointer()
        return p.root_x, p.root_y

    def apertado(self):
        p = self.raiz.query_pointer()
        return bool(p.mask & (1 << 8))  # Button1Mask


class EvdevMouse:
    """Lê o mouse direto de /dev/input (fallback p/ Wayland puro).

    Precisa estar no grupo 'input' ou rodar com sudo.
    Os deltas brutos são convertidos para pixels usando ESCALA (porque o
    evdev não sabe a sensibilidade do mouse — o replay usa a trajetória
    RELATIVA ao clique, então um fator aproximado já funciona).
    """

    def __init__(self, largura, altura):
        import evdev
        from evdev import ecodes
        self.ecodes = ecodes
        self.largura, self.altura = largura, altura
        self.dev = self._achar()
        if self.dev is None:
            raise RuntimeError("nenhum mouse encontrado em /dev/input")
        self.vx, self.vy = largura / 2.0, altura / 2.0
        self.dx = self.dy = 0

    def _achar(self):
        import evdev
        from evdev import ecodes
        candidatos = []
        for path in evdev.list_devices():
            try:
                d = evdev.InputDevice(path)
            except Exception:
                continue
            caps = d.capabilities()
            if ecodes.EV_REL in caps and ecodes.BTN_LEFT in caps.get(ecodes.EV_KEY, []):
                candidatos.append(d)
        if not candidatos:
            return None
        for d in candidatos:
            if "mouse" in d.name.lower():
                return d
        return candidatos[0]

    def ler(self, tempo_max=0.1):
        """Retorna (x, y, apertado) — espera até tempo_max por eventos."""
        from evdev import ecodes
        fim = time.time() + tempo_max
        while time.time() < fim:
            r, _, _ = select.select([self.dev.fd], [], [], 0.02)
            if not r:
                continue
            for ev in self.dev.read():
                if ev.type == ecodes.EV_REL:
                    if ev.code == ecodes.REL_X:
                        self.dx += ev.value
                    elif ev.code == ecodes.REL_Y:
                        self.dy += ev.value
                elif ev.type == ecodes.SYN and ev.code == ecodes.SYN_REPORT:
                    if self.dx or self.dy:
                        self.vx = max(0.0, min(self.largura - 1,
                                               self.vx + self.dx * ESCALA))
                        self.vy = max(0.0, min(self.altura - 1,
                                               self.vy + self.dy * ESCALA))
                        self.dx = self.dy = 0
                elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_LEFT:
                    if ev.value == 1:
                        self.pressionado = True
                    elif ev.value == 0:
                        self.pressionado = False
        return self.vx, self.vy, self.pressionado


def main():
    os.makedirs(PASTA, exist_ok=True)
    largura, altura = tamanho_tela()

    # 1) tenta X11 (sem sudo); 2) fallback evdev (com sudo)
    mouse = None
    modo = ""
    try:
        mouse = X11Mouse()
        modo = "X11"
    except Exception as ex:
        print("[i] X11 indisponível (%s) — tentando evdev..." % ex)
        try:
            mouse = EvdevMouse(largura, altura)
            modo = "evdev"
        except Exception as ex2:
            print("[ERRO] Não consegui ler o mouse (%s)." % ex2)
            print("       Sem X11 e sem permissão em /dev/input.")
            print("       Rode: sudo usermod -aG input $USER  (logout/login)")
            return

    print("=" * 62)
    print("GRAVADOR DE MOVIMENTO DO MOUSE (hCaptcha) — modo %s" % modo)
    print("  Tela: %dx%d" % (largura, altura))
    print("  Apenas mova o mouse NATURALMENTE até o checkbox e clique.")
    print("  Cada clique vira um arquivo em: Captcha/movimentos/")
    print("  Ctrl+C para sair.")
    print("=" * 62)

    buffer = []       # (timestamp, x, y) — histórico rolante do cursor
    botao = False
    clique = None
    soltou_em = None
    total = 0

    def registra_amostra(ts, x, y):
        buffer.append((ts, x, y))
        corte = ts - JANELA
        while buffer and buffer[0][0] < corte:
            buffer.pop(0)

    def finaliza():
        nonlocal botao, clique, buffer, soltou_em, total
        clique_bkp = clique
        botao = False
        clique = None
        soltou_em = None
        snapshot = buffer
        buffer = []

        if not snapshot:
            return
        amostras = [((t - snapshot[0][0]) * 1000, x, y) for (t, x, y) in snapshot]

        # remove a parte parada do início (mão "esperando" antes de partir)
        px0, py0 = amostras[0][1], amostras[0][2]
        while len(amostras) > 1 and \
                (amostras[0][1] - px0) ** 2 + (amostras[0][2] - py0) ** 2 < 64:
            amostras.pop(0)

        if len(amostras) < 5:
            print("[-] Movimento curto demais — descartado. Tente de novo.")
            return
        ddx = amostras[0][1] - clique_bkp["x"]
        ddy = amostras[0][2] - clique_bkp["y"]
        dist = (ddx * ddx + ddy * ddy) ** 0.5
        if dist < MOVIMENTO_MINIMO:
            print("[-] Começou perto demais do checkbox (%.0f px < %d px) —"
                  " mova o mouse de mais longe e clique." %
                  (dist, MOVIMENTO_MINIMO))
            return
        duracao = amostras[-1][0] - amostras[0][0]
        if duracao < DURACAO_MINIMA * 1000:
            print("[-] Gravação muito rápida — descartada. Tente de novo.")
            return

        dados = {
            "amostras": amostras,
            "clique": clique_bkp,
            "tela": {"largura": largura, "altura": altura},
        }
        caminho = salvar(dados)
        total += 1
        print("[+] Salvo: %s (%d amostras, %.2f s, clique %d ms) — total: %d"
              % (os.path.basename(caminho), len(amostras), duracao / 1000.0,
                 clique_bkp["duracao"], total))
        print("    Ctrl+C para sair.")

    try:
        while True:
            if modo == "X11":
                x, y = mouse.posicao()
                apertado = mouse.apertado()
                agora = time.perf_counter()
                registra_amostra(agora, x, y)

                if apertado and not botao:
                    botao = True
                    clique = {"x": x, "y": y, "inicio": agora}
                    print("[+] Clique em (%d, %d)" % (x, y))
                if not apertado and botao and clique and soltou_em is None:
                    clique["duracao"] = (time.perf_counter() - clique["inicio"]) * 1000
                    soltou_em = time.time()

                if soltou_em is not None and (time.time() - soltou_em) >= POS_CLIQUE:
                    finaliza()

                time.sleep(TAXA)
            else:  # evdev
                x, y, apertado = mouse.ler()
                agora = time.perf_counter()
                registra_amostra(agora, x, y)

                if apertado and not botao:
                    botao = True
                    clique = {"x": x, "y": y, "inicio": agora}
                    print("[+] Clique em (%d, %d)" % (x, y))
                if not apertado and botao and clique and soltou_em is None:
                    clique["duracao"] = (time.perf_counter() - clique["inicio"]) * 1000
                    soltou_em = time.time()

                if soltou_em is not None and (time.time() - soltou_em) >= POS_CLIQUE:
                    finaliza()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[+] Fim. Gravações salvas: %d em %s" % (total, PASTA))


if __name__ == "__main__":
    main()