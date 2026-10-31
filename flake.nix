{
  description = "ESPuino MediaHub — local hub for centrally managing the RFID assignments of multiple ESPuinos";

  inputs = {
    flake-parts.url = "github:hercules-ci/flake-parts";
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = inputs @ {flake-parts, ...}:
    flake-parts.lib.mkFlake {inherit inputs;} {
      systems = ["x86_64-linux" "aarch64-linux"];

      flake = {
        nixosModules.default = ./nix/module.nix;
      };

      perSystem = {
        lib,
        pkgs,
        ...
      }: let
        mediahub = pkgs.callPackage ./nix/package.nix {};
        tests = import ./nix/tests.nix {
          inherit lib pkgs;
          nixpkgs = inputs.nixpkgs;
          module = ./nix/module.nix;
        };
      in {
        packages = {
          mediahub = mediahub;
          default = mediahub;
        };

        devShells.default = pkgs.mkShell {
          packages = [mediahub.pythonEnv];
        };

        checks = tests;
      };
    };
}
