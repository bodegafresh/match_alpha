-- Migration 022: Cargar la FIFA draw matrix oficial para mejores terceros
--
-- El resolver usa competition_stages.rules['best_third_assignment_matrix']
-- para asignar cada mejor tercero a su slot correcto.
-- Sin esta matrix, el algoritmo cae a greedy y asigna mal.
--
-- Formato: key = grupos clasificados ordenados (comma-joined, uppercase)
--          value = { slot_code: group_letter }
--
-- Basado en el sorteo oficial del Mundial 2026.
-- Con 6 grupos confirmados (B,D,F,I,J,K) y 2 pendientes (E=Ecuador, L=Ghana).

UPDATE competition_stages
SET rules = coalesce(rules, '{}'::jsonb) || jsonb_build_object(
  'best_third_assignment_matrix',
  '{
    "B,D,F,I,J,K": {
      "third_place_group_a_b_c_d_f": "D",
      "third_place_group_a_e_h_i_j": "I",
      "third_place_group_b_e_f_i_j": "B",
      "third_place_group_c_d_f_g_h": "F",
      "third_place_group_e_f_g_i_j": "J",
      "third_place_group_e_h_i_j_k": "K"
    },
    "B,D,E,F,I,J,K,L": {
      "third_place_group_a_b_c_d_f": "D",
      "third_place_group_a_e_h_i_j": "I",
      "third_place_group_b_e_f_i_j": "B",
      "third_place_group_c_d_f_g_h": "F",
      "third_place_group_c_e_f_h_i": "E",
      "third_place_group_d_e_i_j_l": "L",
      "third_place_group_e_f_g_i_j": "J",
      "third_place_group_e_h_i_j_k": "K"
    }
  }'::jsonb
)
WHERE stage_code = 'ROUND_OF_32';

-- Verificar:
-- SELECT rules->'best_third_assignment_matrix' FROM competition_stages WHERE stage_code = 'ROUND_OF_32';
