# Lessico laboratorio — base Go2 vs braccio D1 (IT)

- **«Alza il cane»**, **«metti il cane in piedi»**, **«rialza il cane»** → comando **Sport** sulla base quadrupede: `base_motion.mode` = **`stand_up`** (non è movimento braccio).
- **«Abbassa / accuccia il cane»** → **`base_motion.mode`** = **`crouch`**.
- **«Ferma il cane»**, **«stop base»** → **`stop`** (Sport).
- Usa **`arm_joint_delta`** / **`arm_preset`** / **`arm_tool_target`** solo se l’operatore parla di **braccio**, **giunto**, **gradi di manipolazione**, **pinza**, **IK / visione**.

Il server può anche correggere automaticamente alcune frasi (vedi routing Hermes); questo file rende le stesse regole **parte della conoscenza stabile** nel system prompt.
