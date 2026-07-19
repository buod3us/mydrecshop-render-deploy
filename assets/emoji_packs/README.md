# Emoji pack registry

The bot's reusable Telegram custom emoji library is stored in `registry.json`.
Each pack entry contains its public link, title, type, numbered emoji list,
`custom_emoji_id`, Unicode fallback, and animation format. Numbered contact
sheets are stored in `previews/`.

## Saved packs

| Pack | Emoji | Format |
| --- | ---: | --- |
| `ApplicationEmoji` | 150 | 150 animated TGS |
| `ADROITPACKE` | 124 | 124 static |
| `IslomjonAnimeEmoji` | 191 | 191 animated TGS |
| `TG_d3536_by_TgEmojisBot` | 198 | 148 animated TGS, 43 video, 7 static |
| `Lumpre_by_fStikBot` | 200 | 86 animated TGS, 114 video |
| `FinanceEmoji` | 60 | 60 animated TGS |
| `Statusvideobytaraxd` | 177 | 177 animated TGS |

Total: 1,100 unique custom emoji.

To select an icon, find its number on the relevant contact sheet, then use the
item with the same `index` in `registry.json`. The `custom_emoji_id` is the value
stored for the product or menu button. The `fallback_emoji` is shown only when
Telegram cannot render the custom icon.
