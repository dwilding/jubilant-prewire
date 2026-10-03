# jubilant-prewire — Test your charm, not your patience

jubilant-prewire is an **experimental** plugin for [Juju charm](https://canonical.com/juju/charms-architecture) integration tests. It modifies Jubilant's [`deploy()`](https://canonical.com/juju/docs/jubilant/reference/jubilant/#jubilant.Juju.deploy) method to pre-pull OCI images before calling the Juju CLI, so timeouts tell you about real problems instead of slow image pulls.

![juju.deploy() in an integration test](editor.svg)

To use jubilant-prewire, add it as an integration testing dependency of your charm:

```text
uv add --group integration git+https://github.com/dwilding/jubilant-prewire@main
```

Then run the integration tests as normal.
