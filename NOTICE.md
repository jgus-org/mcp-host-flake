# Upstream and distributed artifacts

| Artifact                                                                                                                                | Licence & Notifications                                                                              |
| --------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| This flake: `flake.nix` and `host.py` — the whole program — and these documents                                                         | [MIT](LICENSE.md)                                                                                    |
| [mcp](https://github.com/modelcontextprotocol/python-sdk), in the Python environment `packages.mcp-host` (= `packages.default`) carries | [MIT](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/LICENSE), as nixpkgs records it |
| [starlette](https://github.com/encode/starlette), in that same environment                                                              | [BSD-3-Clause](https://opensource.org/license/BSD-3-Clause), as nixpkgs records it                   |

Serving that package from a binary cache conveys the environment with it, and each installed distribution carries its own licence text and SPDX identifier inside the output, at `<site-packages>/<name>-<version>.dist-info/`.
