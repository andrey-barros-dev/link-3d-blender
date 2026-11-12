# Link 3D para Blender

Add-on independente para importar modelos de links públicos do Sketchfab no Blender com materiais, texturas, rigs e animações.

Versão atual: **2.5.3**.

## Instalação

1. Baixe [sketchfab_link_v2-2.5.3.zip](https://github.com/andrey-barros-dev/link-3d-blender/releases/latest/download/sketchfab_link_v2-2.5.3.zip).
2. No Blender, abra **Edit > Preferences > Add-ons > Install from Disk** e selecione o ZIP.
3. Ative **Sketchfab Link V2**. Se tinha a versão antiga, desative **Link 3D para Blender**.

O add-on funciona sem MCP, Node.js ou instalação de pacotes Python. Usa Chrome/Edge instalado ou prepara um navegador portátil automaticamente quando disponível.

## Uso

Na Viewport 3D, pressione **N** e abra **Link 3D V2**:

1. Cole o **Link** do modelo.
2. Escolha a **Pasta de download** pelo ícone de pasta.
3. Clique em **Baixar**.

A opção **Visual do viewer**, desligada por padrão, traz ambiente, fundo, luzes e pós-processamento compatíveis e afeta a cena inteira.

Cada importação acrescenta uma coleção e preserva os modelos anteriores. Há um download por vez. O projeto Blender não é salvo automaticamente.

## Compatibilidade

Testado no Windows com Blender 5.2.1 e 5.2.3 Release Candidate. Requer internet. A detecção de navegador também contempla macOS/Linux, ainda sem teste real nesses sistemas.

Alguns efeitos do viewer têm aproximações no Blender, especialmente refração e pós-processamento. Áudio e primitivas de linhas ainda não são suportados.

Use modelos conforme suas respectivas licenças ou com autorização do autor. A visualização não concede direitos de uso.

## Desenvolvimento

Código próprio em `sketchfab_link_v2/`, sem os scripts ou o conversor da versão anterior.

Testes de transporte e navegador: `python -m unittest discover -s tests -p "test_*.py"`.

Regressões de importação e pasta, em Blender separado:

```sh
blender --background --factory-startup --python-exit-code 1 --python tests/blender_import_fallbacks.py
blender --background --factory-startup --python-exit-code 1 --python tests/blender_download_folder.py
```

Para gerar o ZIP: `python tools/build.py`.

Licença MIT. Consulte [CHANGELOG.md](CHANGELOG.md) para as mudanças recentes.

