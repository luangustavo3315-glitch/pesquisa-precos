# Pesquisa de Preços Tietê

Projeto de pesquisa automatizada de preços desenvolvido em **Python** com **Playwright** para consulta a supermercados da região de Tietê (SP). O sistema usa um catálogo com **262 produtos** e aplica regras para comparar descrições, marcas, quantidades e unidades de venda antes de registrar um preço.

## Funcionalidades

- Consulta automatizada de produtos em estabelecimentos configurados.
- Catálogo de produtos e regras de correspondência para evitar comparações indevidas.
- Coleta de preços e organização dos resultados em planilhas Excel.
- Registro de evidências e informações de execução localmente.
- Possibilidade de limitar a consulta a uma quantidade menor de produtos.

## Estrutura

- `src/price_research/`: implementação da coleta, correspondência de produtos, orquestração e geração de relatórios.
- `config/stores.yaml`: parâmetros e fontes da pesquisa.
- `input/`: catálogo e regras de equivalência de produtos.
- `main.py`: interface principal de execução.
- `executar_todos.py`: execução do catálogo completo pelo Python.
- `setup_windows.ps1` e `run_full.ps1`: scripts auxiliares para Windows.

## Requisitos

- Python instalado no computador.
- Acesso à internet para consultar as páginas dos estabelecimentos.
- Navegador Chromium do Playwright.

## Instalação

No Windows, abra o PowerShell na pasta do projeto e execute:

```powershell
.\setup_windows.ps1
```

Ou instale manualmente:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Execução

Para executar uma consulta limitada, adequada para verificar a configuração:

```powershell
.\.venv\Scripts\python.exe main.py --limit 3
```

Para percorrer todo o catálogo:

```powershell
.\run_full.ps1
```

Os resultados gerados na execução ficam em pastas locais ignoradas pelo Git, como `output/`, `logs/` e `evidencias/`.

## Observações

O programa é um projeto independente e não possui vínculo, autorização institucional ou parceria com as redes comerciais cujos sites são consultados. Respeite os termos de uso dos serviços acessados. A disponibilidade dos produtos e os valores anunciados podem variar, portanto os preços devem ser conferidos antes de qualquer compra.

Este repositório reúne **o código operacional e o catálogo**, sem resultados históricos de consultas, registros de execução, arquivos de testes ou relatórios de validação. O sistema ainda possui rotinas internas de verificação de equivalência e consistência, necessárias para seu funcionamento.