# Histórico de versões

## 2.5.3

- Substitui a implementação antiga pela nova implementação independente, sem MCP, Node.js ou conversor incorporado.
- Painel com Link, Pasta de download, Baixar e a opção Visual do viewer.
- Cada download acrescenta uma coleção, preservando os modelos anteriores.
- Bones exibidos em linhas finas, com comprimentos proporcionais às poses de todos os clipes.
- A pasta escolhida é respeitada também quando já houve outro download na sessão.
- Corrige a rejeição de peças sem UV que compartilham materiais texturizados. Usa as coordenadas constantes do viewer e preserva as texturas.
- Texturas com coordenadas Generated/Object não exigem mapas UV.
- Ausência de amostras opcionais de conferência não cancela animações; o resultado fica registrado como não verificado.
- Mantém materiais PBR, texturas, animações por ossos e morphs, nuvens de pontos e ambiente opcional compatíveis.

## 1.0.0

Implementação anterior, preservada no histórico do repositório e na release original.
