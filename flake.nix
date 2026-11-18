{
  description = "Serve any number of stdio MCP servers behind one streamable-HTTP endpoint";

  inputs = {
    flake-lib = {
      inputs.nixpkgs.follows = "nixpkgs";
      url = "github:jgus-org/flake-lib/v1";
    };
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs =
    {
      self,
      flake-lib,
      nixpkgs,
    }:
    flake-lib.lib.mkPythonFlake {
      inherit nixpkgs self;
      payload.packages =
        { pkgs, ... }:
        rec {
          default = mcp-host;
          mcp-host = flake-lib.lib.mkPythonApplication {
            inherit pkgs;
            dependencies = p: [
              p.mcp
              p.starlette
            ];
            name = "mcp-host";
            pythonPath = [ (flake-lib.lib.pythonDualStackIPv6.site pkgs) ];
            script = ./host.py;
          };
        };
    };
}
