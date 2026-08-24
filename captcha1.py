"""
Consulta de Situação Cadastral do CPF - Receita Federal
https://servicos.receita.fazenda.gov.br/Servicos/CPF/ConsultaSituacao/ConsultaPublica.asp

O site usa hCaptcha. A validação dele (funcoes03.js -> ValidarDados) olha:
  - document.getElementById("h-recaptcha-response").value  (precisa estar preenchido)
  - o campo oculto idCheckedReCaptcha (precisa ser "true")
O hCaptcha só cria a textarea name="h-captcha-response", então espelhamos o token
no campo que a página valida e marcamos idCheckedReCaptcha="true" antes do envio.

Esta versão foca em MELHORAR A REPUTAÇÃO da sessão contra o scoring do hCaptcha:
  1. CDP Mode (dirige o Chrome via CDP, sem o fingerprint do WebDriver);
  2. perfil persistente (cookies/histórico acumulam reputação entre execuções);
  3. clique com mouse real no checkbox (mais humano que clique programático).

REALIDADE: nada disso "resolve" o hCaptcha. Se o desafio de imagens aparecer,
você resolve na janela. E o fator que mais pesa é o SEU IP: IP residencial normal
pontua bem; VPN/proxy/datacenter derruba o score e o servidor recusa o token.
"""

from seleniumbase import SB
import os
import time

from movimento_humano import (
    clicar_checkbox_humano,
    pyautogui_ok,
)

# ===== Ambiente do Chrome =====
CHROME_BINARY = "/opt/google/chrome/google-chrome"
# Perfil dedicado e PERSISTENTE. Quanto mais você usar (e navegar/logar em sites
# comuns dentro dele), melhor a reputação da sessão para o hCaptcha.
PROFILE_DIR = "/home/dpe-robo/selenium/chrome_profile_rf"

# ===== Dados da consulta =====
CPF = os.environ.get("CPF", "")              # 11 dígitos, via env
DATA_NASCIMENTO = os.environ.get("DATA_NASCIMENTO", "")  # ddmmaaaa, via env
URL = "https://servicos.receita.fazenda.gov.br/Servicos/CPF/ConsultaSituacao/ConsultaPublica.asp"


def preencher_campo(sb, campo_id, valor):
    """Digita os dígitos (char a char, mais humano) e dispara o onblur do site."""
    sb.cdp.type("#" + campo_id, valor)
    # .blur() faz o próprio site formatar (FG_FormatarCPF / FG_FormatarData)
    sb.cdp.evaluate(f"document.getElementById('{campo_id}').blur();")
    sb.sleep(0.6)


def token_hcaptcha(sb):
    """Lê o valor da textarea do hCaptcha. Vazio = ainda não resolvido."""
    return sb.cdp.evaluate(
        "(function(){var e=document.querySelector("
        "\"textarea[name='h-captcha-response']\");return e?e.value:'';})();"
    )


def aguardar_token(sb, timeout=180):
    """Espera o hCaptcha gerar o token (checkbox verde ou imagens resolvidas)."""
    fim = time.time() + timeout
    while time.time() < fim:
        if token_hcaptcha(sb):
            return True
        time.sleep(2)  # polling espaçado p/ interferir menos com o hCaptcha
    return False


def preparar_envio(sb):
    """Deixa o DOM no estado que a PÁGINA valida: espelha o token do hCaptcha no
    campo id/name 'h-recaptcha-response' e marca idCheckedReCaptcha='true'.
    Retorna um diagnóstico do estado real do DOM."""
    return sb.cdp.evaluate(r"""
        (function () {
            var ta = document.querySelector("textarea[name='h-captcha-response']");
            var token = ta ? ta.value : "";

            var chk = document.getElementsByName("idCheckedReCaptcha")[0];
            if (chk) chk.value = "true";

            var alvo = document.getElementById("h-recaptcha-response");
            var existiaAlvo = !!alvo;
            if (!alvo) {
                alvo = document.createElement("input");
                alvo.type = "hidden";
                alvo.id = "h-recaptcha-response";
                alvo.name = "h-recaptcha-response";
                (document.getElementById("theForm") || document.body).appendChild(alvo);
            }
            alvo.value = token;

            return {
                token_len: token.length,
                idCheckedReCaptcha: chk ? chk.value : "<sem campo>",
                h_recaptcha_response_existia: existiaAlvo
            };
        })();
    """)


def clicar_checkbox(sb):
    """Clica no checkbox do hCaptcha com MOUSE REAL.

    Usa a trajetória que VOCÊ gravou (Captcha/movimentos/) quando houver;
    caso contrário cai no movimento sintético (curva de Bézier humana)."""
    sb.cdp.wait_for_element_visible(".h-captcha iframe", timeout=15)
    sb.sleep(3)  # espera 3s antes de clicar
    rect = sb.cdp.get_gui_element_rect(".h-captcha iframe")
    x = rect["x"] + 32                 # checkbox ~32px da borda esquerda
    y = rect["y"] + rect["height"] / 2

    clicar_checkbox_humano(pyautogui_ok(), x, y)


def main():
    with SB(
        uc=True,
        locale="pt-BR",
        binary_location=CHROME_BINARY,
        user_data_dir=PROFILE_DIR,
    ) as sb:
        print("[+] Abrindo em CDP Mode (perfil persistente)...")
        sb.activate_cdp_mode(URL)
        sb.sleep(2)

        # 1) Preenche CPF e data
        print("[+] Preenchendo dados do formulário...")
        preencher_campo(sb, "txtCPF", CPF)
        preencher_campo(sb, "txtDataNascimento", DATA_NASCIMENTO)

        # 2) hCaptcha
        print("\n" + "=" * 60)
        print(">>> Marcando o checkbox do hCaptcha (mouse real)... <<<")
        print("    Se abrir o desafio de imagens, resolva na janela.")
        print("=" * 60 + "\n")
        try:
            clicar_checkbox(sb)
            print("[+] Checkbox clicado.")
        except Exception as e:
            print("[-] Falha no clique automático:", e)
            print(">>> Marque o hCaptcha manualmente na janela. <<<")

        print("[+] Aguardando o token (resolva o desafio de imagens se aparecer)...")
        if not aguardar_token(sb, timeout=180):
            print("[-] hCaptcha não resolvido no tempo limite. Abortando envio.")
            input("Pressione Enter para fechar o navegador.")
            return

        info = preparar_envio(sb)
        print("[+] hCaptcha resolvido. Diagnóstico do DOM:")
        for k, v in info.items():
            print(f"      {k}: {v}")

        if not info["token_len"]:
            print("[-] Token vazio mesmo após resolver — algo bloqueou o hCaptcha.")
            input("Pressione Enter para fechar o navegador.")
            return

        # 3) Envia
        print("[+] Enviando formulário...")
        sb.cdp.click("#id_submit")
        sb.sleep(3)

        # 4) Resultado
        print("[+] Consulta enviada. Veja o resultado na tela.")
        input("\nPressione Enter no terminal para fechar o navegador...")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Erro geral na execução:", e)
        input("Pressione Enter para sair.")
