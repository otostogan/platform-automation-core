"""Operator console: the workstation side of the ``platform`` command.

The host side of ``platform`` runs as root and changes state. This package
runs on an operator's machine, holds no credentials of its own, and only ever
drives what already exists: ``platform --json`` on a host over the operator's
own SSH, the collection playbooks, ``gh``, ``sops`` and ``age``.
"""
