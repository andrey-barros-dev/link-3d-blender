# Link 3D para Blender

Add-on para baixar dados de visualizadores Sketchfab e importar modelos na cena do Blender. Mantém texturas e tenta preservar rigs e animações quando compatíveis.

## Instalação

1. Instale o [Node.js LTS](https://nodejs.org/) e reinicie o Blender.
2. Baixe `link-3d-blender-v1.0.0.zip` na página de **Releases** deste repositório.
3. No Blender, abra **Edit > Preferences > Add-ons** e instale o ZIP. Nas versões recentes, use **Get Extensions > Install from Disk**.
4. Ative **Link 3D para Blender**.

## Uso

Na Viewport 3D, pressione **N**, abra **Link 3D**, cole o link, escolha a pasta de saída e clique em **Baixar e importar**. O projeto Blender não é salvo automaticamente.

## Compatibilidade

Testado em Windows com Blender 5.2.1 e Node.js 24. Requer internet e acesso ao visualizador. Outros sistemas e versões ainda não foram testados. Confirme texturas, escala e animações após a importação; modelos privados ou incompatíveis podem falhar.

Use modelos conforme suas respectivas licenças ou com autorização do autor. A visualização não concede direitos de uso.

## Créditos

Conversão baseada em [sfrip](https://github.com/Corpocore/sfrip), sob licença MIT, com ajuste experimental para UVs. Os avisos de licença das dependências acompanham o código.

