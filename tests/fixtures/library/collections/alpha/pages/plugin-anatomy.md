---
title: "Plugin anatomy"
tags: [plugins, authoring, anatomy]
---

Every plugin has three parts: the manifest (identity), the setup function (lifecycle hooks), and the optional cleanup function. Authoring a plugin means wiring these three pieces together.

```ts
export default Plugin.define({
  id: "my-plugin",
  async setup(ctx) {
    ctx.on("session.start", () => { /* … */ })
  }
})
```
