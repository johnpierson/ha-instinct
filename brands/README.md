# Brand assets

`icon.png` (256×256) and `icon@2x.png` (512×512) — the Instinct mark used in
Home Assistant and HACS.

## Making the icon show in HA / HACS

Home Assistant pulls integration icons from the central
[home-assistant/brands](https://github.com/home-assistant/brands) repo, not from
this repo. Until the mark is merged there, HACS shows a generic placeholder (and
the HACS CI check `brands` is intentionally ignored in `.github/workflows`).

To publish it, open a PR to home-assistant/brands adding:

```
custom_integrations/instinct/icon.png      (256x256, from here)
custom_integrations/instinct/icon@2x.png   (512x512, from here)
```

Once merged, remove `ignore: brands` from the validate workflow and the icon
renders automatically.
