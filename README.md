# Consultas de CPF e CNPJ

Todo o fluxo está em `rodar_tudo.py`. O número recebido determina qual site da
Receita Federal será consultado.

```bash
# CNPJ: somente o documento
venv/bin/python rodar_tudo.py 00.394.460/0058-87

# CPF: consulta a data de nascimento no Projudi e depois consulta a Receita
venv/bin/python rodar_tudo.py SEU_CPF

# CPF com data já conhecida: consulta diretamente a Receita
venv/bin/python rodar_tudo.py SEU_CPF 01/01/2000
```

Substitua `SEU_CPF` pelo número que deseja consultar. Os documentos aceitam
pontuação. A data aceita `DD/MM/AAAA`, `DDMMAAAA`, `AAAA-MM-DD` ou `DD-MM-AAAA`.
Datas informadas são opcionais para CPF. O script rejeita datas inválidas e
CNPJ acompanhado de data. Quando o Projudi não encontra uma data válida para
o CPF, a consulta é interrompida antes de acessar a Receita.

## Configuração

Use o ambiente virtual existente ou prepare outro com:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Configure `GEMINI_API_KEY` no `.env` ao lado do script. `GEMINI_MODEL` permite
escolher o modelo. Para executar sem argumentos, configure `CPF` ou `CNPJ`.
Se ambos estiverem configurados, o modo sem argumentos utiliza o CPF e busca
o nascimento no Projudi. Datas antigas em `DATA_NASCIMENTO` no `.env` não são
reaproveitadas: para informar uma data manualmente, passe-a no comando.

A consulta de nascimento usa o script existente
`/home/wagner/Área de trabalho/consulta_nascimento_projudi.py` e o Python de
`/home/wagner/CEAJE_PROTOCOLACAO_ROBO/.venv/bin/python`. O login é o já configurado
no `.env` do robô. Essa etapa roda em um perfil de navegador descartável e os
CSVs temporários são removidos ao terminar.

Para outras instalações, ajuste `PROJUDI_NASCIMENTO_SCRIPT`, `PROJUDI_ROBO_DIR`
e, se necessário, `PROJUDI_PYTHON` no `.env`. O projeto continua com um único
script principal; o consultor de nascimento permanece no local informado.

Também são aceitas as formas explícitas:

```bash
venv/bin/python rodar_tudo.py --cnpj 00.394.460/0058-87
venv/bin/python rodar_tudo.py --cpf SEU_CPF
venv/bin/python rodar_tudo.py --cpf SEU_CPF --data 01/01/2000
```

## Resultado

O script abre uma aba própria no Chrome da automação, resolve o hCaptcha e
confirma que o comprovante corresponde ao documento recebido. Salva PDF, PNG e
`resultado.json` em uma pasta por execução dentro de `output/playwright/cpf/`
ou `output/playwright/cnpj/`.

Use `--saida /caminho/dos/comprovantes` para mudar a pasta e `--tempo-max 180`
para ajustar o limite em segundos. Uma consulta concluída retorna código 0;
falhas retornam 1 e argumentos inválidos retornam 2.
O limite inclui a busca de nascimento no Projudi e a consulta na Receita.
O JSON de CPF registra a data utilizada e sua origem (`projudi` ou `informada`).

O Chrome pessoal e as outras abas permanecem abertos. `ROBO_JANELA=visivel`
exibe a janela da automação quando há uma sessão gráfica disponível.
