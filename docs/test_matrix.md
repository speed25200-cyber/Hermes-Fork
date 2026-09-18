# Matrice de tests T01–T70 (§64)

<!-- Fichier GÉNÉRÉ par `scripts/test_matrix_status.py`. Ne pas éditer à la main : toute
     correction manuelle serait écrasée, et surtout elle ne serait adossée à aucune preuve. -->

Source : `reports/junit.xml` — 825 cas collectés, 825 verts, 0 échecs, 0 erreurs, 0 sautés.

Niveaux présents dans ce rapport : `contract`, `e2e`, `integration`, `property`, `unit`. Les tests `integration` (schéma et migrations sur PostgreSQL) ont été RÉELLEMENT exécutés contre un serveur PostgreSQL 16. La suite ne contient AUCUN test `connected` : rien ici n'a été confronté au vrai OKX ni au vrai service TypeSafe. Toute exigence qui demande un appel réel reste `NOT_RUN` par construction, et un `PASS` sur fixtures ne la remplace pas.

Lecture des statuts :

- `PASS` — au moins un cas nommant l'identifiant a été exécuté et aucun n'a échoué ;
- `FAIL` — au moins un cas a échoué ou est tombé en erreur ;
- `NOT_RUN` — aucun cas ne porte l'identifiant, ou tous ont été sautés. Un test
  sauté n'a rien vérifié : il ne devient pas vert parce que la suite est verte.

Un `PASS` signifie « ce comportement est vérifié sur fixtures hors ligne ». Il ne signifie ni « vérifié contre OKX », ni « rentable » : aucune ligne de ce tableau n'est une mesure de marché.

| ID | Cas à tester (§64) | Résultat attendu (§64) | Niveau | Tests | Statut | Preuve |
|---|---|---|---|---|---|---|
| T01 | Conversion contrats/base/notionnel | Unités et signe exacts sur fixtures linéaires | unit | `test_domain_instruments::test_T01_contract_base_notional_conversions` | **PASS** | 1 cas vert |
| T02 | Instrument inverse ou devise non supportée | Rejet explicite avant modèle/ordre | unit | `test_domain_instruments::test_T02_inverse_options_and_foreign_settlement_are_rejected` | **PASS** | 1 cas vert |
| T03 | Tick/lot non admissible | Normalisation contrôlée ou rejet, puis revalidation du risque | unit | `test_domain_instruments::test_T03_lot_and_min_size_rounding_and_validation` | **PASS** | 1 cas vert |
| T04 | Métadonnée modifiée en cours de session | Nouvelle version appliquée sans interpréter le passé avec elle | unit | `test_domain_instruments::test_T04_metadata_version_change_does_not_rewrite_past`<br>`test_point_in_time::test_T04_instrument_versions_are_read_at_their_observation_date` | **PASS** | 2 cas verts |
| T05 | Snapshot suivi d'updates valides | Book conforme au résultat de référence | unit | `test_orderbook::test_T05_snapshot_then_valid_updates_match_reference` | **PASS** | 1 cas vert |
| T06 | Trou de séquence | Book invalide et aucune nouvelle entrée concernée | unit | `test_orderbook::test_T06_sequence_gap_invalidates_and_blocks` | **PASS** | 1 cas vert |
| T07 | Séquences valides mais non consécutives de un | Pas de faux rejet | unit | `test_orderbook::test_T07_non_consecutive_sequences_are_not_false_rejections` | **PASS** | 1 cas vert |
| T08 | Message de maintien sans changement | Santé et ancienneté correctement distinguées | unit | `test_orderbook::test_T08_heartbeat_distinguishes_staleness_from_connection_health` | **PASS** | 1 cas vert |
| T09 | Reset de séquence | Comportement conforme au validateur de protocole, sinon resync | unit | `test_orderbook::test_T09_sequence_reset_requires_new_snapshot` | **PASS** | 1 cas vert |
| T10 | Checksum déprécié fixé à zéro | Aucun calcul CRC incorrect sur le flux concerné | unit | `test_orderbook::test_T10_deprecated_checksum_is_never_computed` | **PASS** | 1 cas vert |
| T11 | Quantité de niveau égale à zéro | Niveau supprimé et top-of-book reconstruit | unit | `test_orderbook::test_T11_zero_quantity_removes_level_and_rebuilds_top` | **PASS** | 1 cas vert |
| T12 | Carnet croisé, quantité négative ou NaN | Données invalides exclues des décisions | unit | `test_orderbook::test_T12_crossed_negative_and_nan_are_invalid` | **PASS** | 1 cas vert |
| T13 | REST et WS non raccordables | Fusion interdite | unit | `test_orderbook::test_T13_rest_snapshot_cannot_be_spliced_with_ws_increments` | **PASS** | 1 cas vert |
| T14 | Donnée future ajoutée à l'historique | Décisions antérieures inchangées | unit | `test_point_in_time::test_T14_future_data_does_not_change_earlier_answers`<br>`test_research_training::test_T14_une_feature_disponible_apres_la_decision_est_refusee` | **PASS** | 2 cas verts |
| T15 | Événement ancien reçu tard | Indisponible avant sa réception réelle | unit | `test_point_in_time::test_T15_old_event_received_late_is_unavailable_before_its_reception` | **PASS** | 1 cas vert |
| T16 | Feature intrabougie vs bougie clôturée | Pas de confusion ni de fuite de clôture | unit | `test_normalizer_archive::test_T16_closed_candle_and_intrabar_candle_are_not_confused` | **PASS** | 1 cas vert |
| T17 | Normalisation ou sélection sur test | Détectée par les assertions de provenance | unit | `test_research_training::test_T17_une_transformation_ajustee_sur_le_test_est_detectee` | **PASS** | 1 cas vert |
| T18 | Actif délisté/renommé | Univers point-in-time et position encore comptabilisée | unit | `test_point_in_time::test_T18_delisted_asset_leaves_universe_but_stays_accountable` | **PASS** | 1 cas vert |
| T19 | Labels chevauchants | Purge et disponibilités temporelles correctes | unit | `test_research_training::test_T19_aucune_ligne_dentrainement_ne_connait_la_periode_de_test`<br>`test_research_training::test_T19_les_lignes_qui_chevauchent_le_test_sont_purgees` | **PASS** | 2 cas verts |
| T20 | Stacking OOF | Aucun entraînement sur la fenêtre prédite | unit | `test_research_stacking::test_T20_un_composant_qui_connait_la_fenetre_predite_est_refuse`<br>`test_research_training::test_T20_les_predictions_oof_precedent_leur_propre_entrainement` | **PASS** | 2 cas verts |
| T21 | Label censuré | Exclusion/masque explicite, pas rendement nul inventé | unit | `test_research_training::test_T21_les_labels_censures_ne_sont_jamais_remplaces_par_zero` | **PASS** | 1 cas vert |
| T22 | Changement du test final après consultation | Statut indépendant perdu et revalidation requise | unit | `test_research_registry::test_T22_la_premiere_consultation_du_test_final_le_consomme`<br>`test_research_registry::test_T22_aucune_optimisation_apres_consultation_du_test_final`<br>`test_research_registry::test_T22_une_autre_execution_du_meme_plan_ne_rend_pas_la_periode_independante` | **PASS** | 3 cas verts |
| T23 | Frais maker/taker et rebate | Signes, devises et montants exacts | unit | `test_costs_fees::test_T23_les_frais_maker_et_taker_sont_des_debits_du_montant_exact`<br>`test_costs_fees::test_T23_un_taux_maker_negatif_est_un_rebate_encaisse_jamais_efface_par_abs`<br>`test_costs_fees::test_T23_le_cout_de_decision_est_loppose_du_flux_de_commission`<br>… (+7) | **PASS** | 10 cas verts |
| T24 | Prix exécutables + spread déduit à nouveau | Erreur de double comptage bloquée | unit | `test_costs_fees::test_T24_le_spread_contenu_dans_le_prix_ne_peut_etre_deduit_une_seconde_fois`<br>`test_costs_fees::test_T24_la_convention_executable_deduit_les_couts_non_contenus_dans_le_prix`<br>`test_costs_fees::test_T24_la_convention_executable_doit_declarer_le_spread_comme_inclus`<br>… (+7) | **PASS** | 10 cas verts |
| T25 | Funding traversé ou non | Flux uniquement aux règlements concernés | unit | `test_costs_funding::test_T25_un_reglement_traverse_produit_le_flux_signe_exact`<br>`test_costs_funding::test_T25_un_reglement_non_traverse_ne_produit_aucun_flux`<br>`test_costs_funding::test_T25_un_short_recoit_le_funding_quand_les_longs_paient`<br>… (+8) | **PASS** | 11 cas verts |
| T26 | Funding final utilisé comme feature antérieure | Fuite détectée | unit | `test_costs_funding::test_T26_le_taux_final_employe_comme_feature_anterieure_est_detecte_comme_fuite`<br>`test_costs_funding::test_T26_un_taux_connaissable_a_la_coupure_nest_pas_une_fuite`<br>`test_costs_funding::test_T26_la_decision_emploie_le_taux_connu_a_son_instant_et_le_signale_sinon`<br>… (+5) | **PASS** | 8 cas verts |
| T27 | Ordre maker non exécuté | Pas de position/PnL fictif | unit | `test_virtual_exchange::test_T27_T28_maker_order_needs_volume_not_a_touch` | **PASS** | 1 cas vert |
| T28 | Toucher d'un prix sans volume suffisant | Pas de fill maker garanti | unit | `test_virtual_exchange::test_T27_T28_maker_order_needs_volume_not_a_touch` | **PASS** | 1 cas vert |
| T29 | Liquidité insuffisante pour un IOC | Fill partiel et reliquat non inventé | unit | `test_virtual_exchange::test_T29_ioc_partial_fill_cancels_the_remainder` | **PASS** | 1 cas vert |
| T30 | Deux ordres consomment la même profondeur | Pas de double allocation du volume simulé | unit | `test_virtual_exchange::test_T30_two_orders_never_consume_the_same_depth` | **PASS** | 1 cas vert |
| T31 | Fill pendant une annulation | Position et frais mis à jour une seule fois | unit | `test_virtual_exchange::test_T31_fill_during_cancellation_counts_once_and_is_not_retroactive` | **PASS** | 1 cas vert |
| T32 | ACK perdu après envoi | UNKNOWN, réservation maintenue, aucun retry aveugle | unit | `test_virtual_exchange::test_T32_lost_ack_leaves_the_order_existing_and_raises` | **PASS** | 1 cas vert |
| T33 | Redémarrage avec UNKNOWN | Réconciliation avant nouvelle entrée | unit | `test_startup_supervisor::test_T33_startup_stops_at_reconciliation_failure` | **PASS** | 1 cas vert |
| T34 | Message/fill dupliqué | Ledger et position idempotents | unit | `test_virtual_exchange::test_T34_duplicate_fills_are_delivered_and_deduplicated_downstream` | **PASS** | 1 cas vert |
| T35 | Fill reçu avant ACK | État cohérent sans perte de l'exécution | unit | `test_virtual_exchange::test_T35_fill_can_arrive_before_the_local_ack` | **PASS** | 1 cas vert |
| T36 | Réutilisation d'un client ID terminal | Interdite par notre journal | unit | `test_execution_client_ids::test_T36_un_client_order_id_deja_terminal_ne_peut_pas_etre_reutilise`<br>`test_execution_client_ids::test_T36_un_client_order_id_encore_vivant_est_refuse_aussi`<br>`test_execution_client_ids::test_T36_un_identifiant_neuf_reste_accepte`<br>… (+2) | **PASS** | 5 cas verts |
| T37 | Réponse partiellement réussie | Traitement item par item | unit | `test_execution_partial_response::test_T37_un_lot_partiellement_reussi_nest_ni_un_succes_ni_un_echec_global`<br>`test_execution_partial_response::test_T37_le_code_dun_item_absent_vaut_succes_et_un_item_refuse_porte_le_sien`<br>`test_execution_partial_response::test_T37_un_lot_partiellement_reussi_est_traite_item_par_item`<br>… (+8) | **PASS** | 11 cas verts |
| T38 | Rejet de reduce-only | Pas de retry sans cette protection | unit | `test_virtual_exchange::test_T38_reduce_only_without_position_is_refused` | **PASS** | 1 cas vert |
| T39 | Passage long -> short | Clôture et nouvelle ouverture distinguées, risques revérifiés | unit | `test_domain_positions::test_T39_long_to_short_flip_splits_close_and_open` | **PASS** | 1 cas vert |
| T40 | Rounding casse la neutralité/marge | Candidat corrigé sous contraintes ou rejeté | unit | `test_portfolio_rounding::test_T40_rounding_that_breaks_neutrality_is_repaired_under_constraints`<br>`test_portfolio_rounding::test_T40_an_admissible_rounded_candidate_passes_untouched`<br>`test_portfolio_rounding::test_T40_rounding_that_breaks_the_exact_margin_is_repaired_under_constraints`<br>… (+8) | **PASS** | 11 cas verts |
| T41 | Solveur timeout/infeasible/NaN | Aucune stratégie de secours non validée | unit | `test_portfolio_optimizer::test_T41_a_failed_solve_keeps_the_previous_allocation_never_the_biggest_scores`<br>`test_portfolio_optimizer::test_T41_an_inaccurate_solution_is_reported_and_never_adopted`<br>`test_portfolio_optimizer::test_T41_non_finite_weights_never_reach_the_target`<br>… (+6) | **PASS** | 12 cas verts |
| T42 | Une seule jambe d'un basket est exécutée | Risque transitoire plafonné et réconciliation | unit | `test_portfolio_optimizer::test_T42_a_single_leg_of_a_neutral_basket_is_reported_as_transient_risk`<br>`test_portfolio_optimizer::test_T42_a_basket_whose_single_leg_stays_inside_the_limits_is_not_flagged`<br>`test_portfolio_optimizer::test_T42_legs_that_do_not_move_are_not_counted_as_transient_risk`<br>… (+3) | **PASS** | 6 cas verts |
| T43 | Ordres opposés/UNKNOWN en attente | Exposition pessimiste prise en compte | unit | `test_risk_budgets::test_T43_opposite_pending_orders_never_offset_each_other`<br>`test_risk_budgets::test_T43_net_exposure_takes_the_worst_of_the_two_worlds_across_instruments`<br>`test_risk_budgets::test_T43_every_non_terminal_state_counts_as_potentially_active`<br>… (+5) | **PASS** | 17 cas verts |
| T44 | Risque journalier après redémarrage | Pertes et halt conservés | unit | `test_composition::test_T44_a_restart_never_resets_a_persisted_halt_or_daily_loss` | **PASS** | 1 cas vert |
| T45 | Dépôt/retrait externe | Performance et high-water mark non artificiellement améliorés | unit | `test_accounting_external_cashflow::test_T45_a_deposit_moves_cash_and_external_cumulative_but_no_pnl_account`<br>`test_accounting_external_cashflow::test_T45_strategy_pnl_separates_a_real_gain_from_a_capital_contribution`<br>`test_accounting_external_cashflow::test_T45_a_withdrawal_is_not_a_loss`<br>… (+11) | **PASS** | 14 cas verts |
| T46 | Deux gateways concurrents | Un seul chemin de signature/envoi effectif | unit | `test_risk_approvals::test_T46_only_one_of_two_concurrent_gateways_holds_a_send_path`<br>`test_risk_approvals::test_T46_a_follower_that_takes_over_gets_a_strictly_higher_fencing_token` | **PASS** | 2 cas verts |
| T47 | Perte du bail/base | Ancien writer incapable d'augmenter le risque | unit | `test_risk_approvals::test_T47_a_former_writer_whose_lease_was_stolen_can_no_longer_send`<br>`test_risk_approvals::test_T47_losing_the_database_makes_the_writer_stand_down_instead_of_guessing`<br>`test_risk_approvals::test_T47_an_unreachable_database_refuses_the_send_at_the_assertion_point`<br>… (+2) | **PASS** | 5 cas verts |
| T48 | Intention/approbation expirée | Ordre non envoyé | unit | `test_risk_approvals::test_T48_an_expired_approval_never_reaches_the_exchange`<br>`test_risk_approvals::test_T48_a_fresh_approval_does_reach_the_exchange_with_its_fencing_token`<br>`test_risk_approvals::test_T48_the_gateway_obeys_a_refusing_approval_rechecker`<br>… (+2) | **PASS** | 5 cas verts |
| T49 | Modification d'un payload approuvé | Hash invalide, nouvelle approbation nécessaire | unit | `test_contracts::test_T49_modified_payload_invalidates_approval` | **PASS** | 1 cas vert |
| T50 | Panne JEV | Risque/protection actifs, fallback explicitement validé ou halt | unit | `test_jev_cache_worker::test_T50_provider_outage_gives_explicit_reason_and_touches_nothing_else`<br>`test_jev_cache_worker::test_T50_missing_key_and_disabled_config_are_explicit_without_calls` | **PASS** | 2 cas verts |
| T51 | Réponse JEV tardive | Exclue du snapshot passé et non antidatée | unit | `test_jev_cache_worker::test_T51_late_response_is_archived_late_excluded_from_past_snapshots_never_backdated`<br>`test_jev_cache_worker::test_T51_contract_refuses_backdated_evaluations` | **PASS** | 2 cas verts |
| T52 | Probabilités JEV invalides | Rejet typé, sans ordre déclenché | contract/unit | `test_typesafe_contract::test_every_documented_invalid_response_is_refused`<br>`test_jev_schemas::test_T52_out_of_range_boolean_or_missing_probability_rejected`<br>`test_jev_schemas::test_T52_nan_and_infinity_rejected_in_json_and_python_objects` | **PASS** | 16 cas verts |
| T53 | Version JEV inattendue | Nouvelle validation requise | contract/unit | `test_typesafe_contract::test_every_documented_invalid_response_is_refused`<br>`test_jev_schemas::test_T53_inconsistent_sum_unknown_category_version_or_missing_distribution_rejected`<br>`test_jev_schemas::test_T53_sum_tolerance_is_explicit`<br>… (+1) | **PASS** | 41 cas verts |
| T54 | Ticker ambigu ou source contradictoire | Mapping non inventé, qualité explicite | unit | `test_jev_mapping::test_T54_ambiguous_ticker_yields_no_mapping_and_explicit_quality`<br>`test_jev_mapping::test_T54_ticker_alone_is_not_an_identity` | **PASS** | 2 cas verts |
| T55 | Injection dans un document | Aucune permission, aucun secret ni action arbitraire accessibles | unit | `test_jev_sources::test_T55_prompt_injection_in_a_document_is_data_only` | **PASS** | 1 cas vert |
| T56 | URL vers réseau privé ou redirection malveillante | SSRF bloquée | unit | `test_jev_sources::test_T56_non_public_addresses_are_refused`<br>`test_jev_sources::test_T56_public_addresses_are_accepted`<br>`test_jev_sources::test_T56_metadata_or_private_resolution_is_blocked_before_any_connection`<br>… (+2) | **PASS** | 28 cas verts |
| T57 | Document rejoué depuis cache | Âge et provenance initiaux conservés | unit | `test_jev_cache_replay::test_T57_a_cached_evaluation_keeps_its_original_timestamps`<br>`test_jev_cache_replay::test_T57_a_cached_evaluation_keeps_its_provenance`<br>`test_jev_cache_replay::test_T57_the_document_age_is_measured_from_publication_not_from_the_cache_read`<br>… (+6) | **PASS** | 12 cas verts |
| T58 | Source ou publication manquante | Absence explicitée, jamais timestamp inventé | unit | `test_jev_sources::test_T58_missing_date_yields_none_never_collection_time`<br>`test_jev_sources::test_T58_explicit_datetime_parsing_never_invents` | **PASS** | 13 cas verts |
| T59 | Stop prévu mais non confirmé | Position marquée non protégée et alerte/réduction selon politique | unit | `test_protections_and_watchdog::test_T59_a_confirmed_stop_protects_the_position`<br>`test_protections_and_watchdog::test_T59_a_stop_not_accepted_leaves_the_position_marked_unprotected`<br>`test_protections_and_watchdog::test_T59_an_exchange_error_while_placing_the_stop_is_not_a_protection`<br>… (+3) | **PASS** | 8 cas verts |
| T60 | Cancel All After déclenché | Ordres concernés traités, position non supposée fermée | unit | `test_health_metrics::test_T60_gateway_connected_but_not_reconciled_is_not_ready`<br>`test_virtual_exchange::test_T60_cancel_all_after_cancels_orders_but_closes_no_position` | **PASS** | 2 cas verts |
| T61 | Processus stratégie mort, heartbeat indépendant vivant | Pas de maintien aveugle de prises de risque orphelines | unit | `test_protections_and_watchdog::test_T61_a_heartbeat_without_observed_state_is_refused`<br>`test_protections_and_watchdog::test_T61_a_heartbeat_carrying_a_dead_strategy_is_refused`<br>`test_protections_and_watchdog::test_T61_a_stale_decision_makes_the_strategy_unhealthy_even_if_it_claims_to_be_alive`<br>… (+1) | **PASS** | 4 cas verts |
| T62 | Exchange indisponible pendant flatten | État pending et exposition résiduelle visible | unit | `test_virtual_exchange::test_T62_unreachable_exchange_and_residual_exposure_are_visible` | **PASS** | 1 cas vert |
| T63 | DEMO en échec | Aucun basculement réseau/clés vers LIVE | unit | `test_demo_never_falls_back_to_live::test_T63_no_failure_mode_turns_a_demo_adapter_into_a_live_one`<br>`test_demo_never_falls_back_to_live::test_T63_a_demo_failure_never_reaches_another_region_domain`<br>`test_demo_never_falls_back_to_live::test_T63_the_demo_flag_cannot_be_flipped_after_construction`<br>… (+4) | **PASS** | 11 cas verts |
| T64 | LIVE sans approbations complètes | Refus avant connexion privée de trading | unit | `test_config::test_T64_live_without_manifest_is_refused`<br>`test_config::test_T64_live_manifest_must_be_signed_complete_and_unexpired`<br>`test_demo_never_falls_back_to_live::test_T63_T64_reaching_live_still_requires_an_authorization` | **PASS** | 3 cas verts |
| T65 | Action UI non autorisée/CSRF | Aucun effet et événement d'audit | unit | `test_api_csrf_and_roles::test_T65_an_anonymous_command_is_refused_and_writes_nothing`<br>`test_api_csrf_and_roles::test_T65_a_reader_cannot_request_a_flatten_and_writes_nothing`<br>`test_api_csrf_and_roles::test_T65_an_operator_can_request_a_flatten_and_it_is_only_a_request`<br>… (+6) | **PASS** | 9 cas verts |
| T66 | Secret dans logs/artefacts/image | Test de sécurité échoué et livraison bloquée | unit | `test_logging_secrets::test_T66_injected_secret_never_appears_in_structlog_output`<br>`test_logging_secrets::test_T66_stdlib_logs_from_uvicorn_are_masked_too`<br>`test_logging_secrets::test_T66_secret_inside_exception_text_is_masked`<br>… (+1) | **PASS** | 4 cas verts |
| T67 | Restauration de sauvegarde | Données restaurées et réconciliation avant reprise | unit | `test_backup_restore_gates::test_T67_a_sound_archive_is_declared_usable_and_writes_nothing`<br>`test_backup_restore_gates::test_T67_verification_does_not_require_the_full_stack`<br>`test_backup_restore_gates::test_T67_a_single_flipped_byte_is_refused_before_any_write`<br>… (+5) | **PASS** | 8 cas verts |
| T68 | Saturation disque/queue/CPU | Backpressure/arrêt contrôlé, pas de perte silencieuse critique | unit | `test_saturation_backpressure::test_T68_the_queue_never_grows_beyond_its_bound`<br>`test_saturation_backpressure::test_T68_dropped_messages_are_counted_and_attributed`<br>`test_saturation_backpressure::test_T68_publishing_never_blocks_on_a_full_queue`<br>… (+5) | **PASS** | 8 cas verts |
| T69 | Tests hors ligne sans réseau | Parcours complet sur fixtures reproductible | unit | `test_normalizer_archive::test_T69_golden_dataset_replays_offline_with_verified_checksum` | **PASS** | 1 cas vert |
| T70 | Même dataset/config/seed | Résultat identique dans la tolérance documentée | unit | `test_jev_ablation::test_T70_lablation_est_reproductible`<br>`test_research_stacking::test_T70_meme_graine_memes_predictions_de_meta_modele`<br>`test_research_training::test_T70_meme_jeu_meme_graine_memes_resultats` | **PASS** | 3 cas verts |

## Synthèse

| Statut | Nombre sur 70 |
|---|---|
| `FAIL` | 0 |
| `NOT_RUN` | 0 |
| `PASS` | 70 |

Aucune exigence en échec dans cette exécution.

**Exigences non exécutées (0)** : aucune.

Chacune reste bloquante pour la capacité qu'elle devait valider (§71.1) : rien ici n'est présenté comme couvert par autre chose.

## Ce que ce tableau ne dit pas

- Les tests de propriété livrés (`tests/property/`) portent sur la conservation d'une position, la direction et la grille des arrondis, l'arithmétique monétaire, les invariants de carnet, l'idempotence du remplacement d'un niveau et la monotonie des séquences. Ils ne nomment aucun identifiant §64 : ils n'apparaissent donc sur aucune ligne, et les propriétés exigées par §64 qui manquent encore (invariants d'exposition, déduplication, monotonie des quantités exécutées, impossibilité d'un ordre sans approbation) ne sont pas couvertes ici.
- Aucun test connecté (clé OKX DEMO/LIVE, clé TypeSafe) n'a été exécuté : ni le connecteur privé, ni un appel JEV réel ne sont vérifiés ici.
- Les chiffres produits par les jeux de données synthétiques ou golden servent à exercer les pipelines. Ils ne constituent aucune preuve d'avantage de marché.

- Les tests frontend (`node --test frontend/tests/*.test.js`, cible `make ui-test`) ne passent pas par pytest : ils ne sont pas dans ce rapport et ne comptent sur aucune ligne.

## Régénération

`reports/` n'est pas versionné : le rapport source doit être reproduit avant de régénérer ce
fichier, sinon le tableau décrirait une exécution que personne ne peut retrouver.

```sh
# Les tests `integration` exigent une base PostgreSQL désignée ; sans elle ils se sautent, et
# les exigences correspondantes retombent à NOT_RUN — ce que le document dira alors.
export OKXQ_TEST_DATABASE_URL=postgresql+psycopg://okxq@127.0.0.1:5432/okxq_test
pytest -q -p no:randomly --junit-xml=reports/junit.xml -p no:cacheprovider
python scripts/test_matrix_status.py --junit reports/junit.xml --out docs/test_matrix.md
```
