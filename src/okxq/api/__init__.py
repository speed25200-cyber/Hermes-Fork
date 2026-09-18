"""API HTTP versionnée (``/api/v1``) et couche de compatibilité de l'interface Hermes conservée (D1).

Principes :
- lecture seule sur la base ; la seule écriture est l'``OperatorAction`` auditée que le runtime exécute ;
- aucune route n'active LIVE (D5) ; aucune clé d'exchange ne transite par l'API ;
- « Non disponible » (``null``) pour toute métrique inconnue, jamais zéro.
"""
