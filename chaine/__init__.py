"""Chaine de verification M1 a M6 (lot fondation).

Le paquet ne fait jamais ``import server`` : c'est le serveur qui se branche
lui-meme a la fin de ``server.py`` (lot integration) ::

    import chaine; chaine.brancher(sys.modules[__name__])

Dans les tests : ``import server as s`` puis ``chaine.brancher(s)``.
"""
from chaine import noyau

VERSION = '0.1.0'


def brancher(module_serveur):
    """Rattache le module serveur a la chaine. Idempotent.

    Rebrancher le meme module ne fait rien ; brancher un autre module
    (rechargement, tests) remplace le precedent. Ne cree aucune table :
    l'initialisation du schema reste explicite (``chaine.schema.init``).
    Renvoie le module branche.
    """
    return noyau.brancher(module_serveur)
