#!/usr/bin/env python3
"""
Genera il modello MuJoCo Go2 + braccio Z1 (placeholder per D1).

Clona mujoco_menagerie se manca, copia asset, crea go2_d1/scene.xml.
"""

import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MENAGERIE_DIR = os.path.join(PROJECT_ROOT, "mujoco_menagerie")
Z1_DIR = os.path.join(MENAGERIE_DIR, "unitree_z1")
GO2_DIR = os.path.join(PROJECT_ROOT, "unitree_mujoco", "unitree_robots", "go2")
GO2_D1_DIR = os.path.join(PROJECT_ROOT, "unitree_mujoco", "unitree_robots", "go2_d1")


# Snippet XML del braccio Z1 da inserire in base_link (posizione payload Go2)
ARM_BODY_XML = '''
      <!-- Braccio Z1 (placeholder D1) montato su piastra payload -->
      <body name="arm_link00" pos="0.15 0 0.06" childclass="z1">
        <inertial pos="-0.00334984 -0.00013615 0.0249584" quat="-0.00692194 0.682592 0.00133293 0.730766" mass="0.472475"
          diaginertia="0.000531375 0.000415207 0.000378658"/>
        <geom class="z1_visual" mesh="z1_Link00"/>
        <geom size="0.0325 0.0255" pos="0 0 0.0255" class="z1_collision"/>
        <body name="arm_link01" pos="0 0 0.0585">
          <inertial pos="2.47e-06 -0.00025198 0.0231717" quat="0.708578 0.705633 0.000281462 -0.000355927" mass="0.673326"
            diaginertia="0.00128328 0.000839362 0.000719308"/>
          <joint name="arm_joint1" axis="0 0 1" range="-2.61799 2.61799"/>
          <geom class="visual" mesh="z1_Link01"/>
          <body name="arm_link02" pos="0 0 0.045">
            <inertial pos="-0.110126 0.00240029 0.00158266" quat="0.00748058 0.707092 -0.0114473 0.70699" mass="1.19132"
              diaginertia="0.0246612 0.0243113 0.00100468"/>
            <joint name="arm_joint2" axis="0 1 0" range="0 2.96706" damping="2"/>
            <geom class="z1_visual" mesh="z1_Link02"/>
            <geom size="0.0325 0.051" quat="1 1 0 0" class="z1_collision"/>
            <geom size="0.0225 0.1175" pos="-0.1625 0 0" quat="1 0 1 0" class="z1_collision"/>
            <geom size="0.0325 0.0255" pos="-0.35 0 0" quat="1 1 0 0" class="z1_collision"/>
            <body name="arm_link03" pos="-0.35 0 0">
              <inertial pos="0.106092 -0.00541815 0.0347638" quat="0.540557 0.443575 0.426319 0.573839" mass="0.839409"
                diaginertia="0.00954365 0.00938711 0.000558432"/>
              <joint name="arm_joint3" axis="0 1 0" range="-2.87979 0"/>
              <geom class="visual" mesh="z1_Link03"/>
              <geom size="0.02 0.058" pos="0.128 0 0.055" quat="1 0 1 0" class="collision"/>
              <geom size="0.0325 0.0295" pos="0.2205 0 0.055" quat="0.5 -0.5 0.5 0.5" class="collision"/>
              <body name="arm_link04" pos="0.218 0 0.057">
                <inertial pos="0.0436668 0.00364738 -0.00170192" quat="0.0390835 0.726445 -0.0526787 0.684087"
                  mass="0.564046" diaginertia="0.000981656 0.00094053 0.000302655"/>
                <joint name="arm_joint4" axis="0 1 0" range="-1.51844 1.51844"/>
                <geom class="z1_visual" mesh="z1_Link04"/>
                <geom size="0.0325 0.0335" pos="0.072 0 0" class="z1_collision"/>
                <body name="arm_link05" pos="0.07 0 0">
                  <inertial pos="0.0312153 0 0.00646316" quat="0.462205 0.535209 0.53785 0.45895" mass="0.389385"
                    diaginertia="0.000558961 0.000547317 0.000167332"/>
                  <joint name="arm_joint5" axis="0 0 1" range="-1.3439 1.3439"/>
                  <geom class="visual" mesh="z1_Link05"/>
                  <body name="arm_link06" pos="0.0492 0 0">
                    <inertial pos="0.0241569 -0.00017355 -0.00143876" quat="0.998779 0.0457735 -0.00663717 0.0173548"
                      mass="0.288758" diaginertia="0.00018333 0.000147464 0.000146786"/>
                    <joint name="arm_joint6" axis="1 0 0" range="-2.79253 2.79253"/>
                    <geom class="z1_visual" mesh="z1_Link06"/>
                    <geom size="0.0325 0.0255" pos="0.0255 0 0" quat="1 0 1 0" class="z1_collision"/>
                  </body>
                </body>
              </body>
            </body>
          </body>
        </body>
      </body>
'''


def main():
    print("Build Go2 + braccio Z1 (placeholder D1)")
    print("=" * 50)

    # 1. Clone mujoco_menagerie
    if not os.path.isdir(MENAGERIE_DIR):
        print("Clonazione mujoco_menagerie...")
        subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/google-deepmind/mujoco_menagerie.git", MENAGERIE_DIR],
            check=True,
            cwd=PROJECT_ROOT,
        )
    else:
        print("mujoco_menagerie già presente")

    if not os.path.isdir(Z1_DIR):
        print("ERRORE: unitree_z1 non trovato in mujoco_menagerie")
        sys.exit(1)

    # 2. Crea go2_d1 e copia asset
    os.makedirs(GO2_D1_DIR, exist_ok=True)
    assets_go2_d1 = os.path.join(GO2_D1_DIR, "assets")
    assets_arm = os.path.join(assets_go2_d1, "arm")
    os.makedirs(assets_arm, exist_ok=True)

    # Copia asset Go2
    for f in os.listdir(os.path.join(GO2_DIR, "assets")):
        src = os.path.join(GO2_DIR, "assets", f)
        if os.path.isfile(src):
            import shutil
            shutil.copy2(src, os.path.join(assets_go2_d1, f))

    # Copia asset Z1
    for f in os.listdir(os.path.join(Z1_DIR, "assets")):
        if f.endswith(".stl"):
            src = os.path.join(Z1_DIR, "assets", f)
            import shutil
            shutil.copy2(src, os.path.join(assets_arm, f))

    print("Asset copiati")

    # 3. Leggi go2.xml e inserisci braccio
    go2_xml_path = os.path.join(GO2_DIR, "go2.xml")
    with open(go2_xml_path, "r") as f:
        go2_content = f.read()

    # Inserisci arm dopo RR_hip, prima della chiusura di base_link
    marker = "      </body>\n    </body>\n  </worldbody>"
    if marker not in go2_content:
        marker = "      </body>\n    </body>"
    if marker in go2_content:
        # Inserisci arm dopo il primo </body> del marker
        replacement = "      </body>" + ARM_BODY_XML + "\n    </body>\n  </worldbody>"
        go2_content = go2_content.replace(marker, replacement)
    else:
        try:
            # Fallback: inserimento prima di </body> di base_link
            idx = go2_content.rfind("    </body>\n  </worldbody>")
            if idx >= 0:
                insertion = ARM_BODY_XML + "\n    "
                go2_content = go2_content[:idx] + insertion + go2_content[idx:]
            else:
                raise ValueError("Pattern non trovato")
        except Exception as e:
            print("ERRORE inserimento:", e)
            sys.exit(1)

    # Aggiungi asset mesh Z1 (sempre, per riferimento geom mesh="z1_Link00" ecc.)
    if 'mesh file="arm/z1_Link00.stl"' not in go2_content:
        asset_insert = """
    <mesh file="arm/z1_Link00.stl" />
    <mesh file="arm/z1_Link01.stl" />
    <mesh file="arm/z1_Link02.stl" />
    <mesh file="arm/z1_Link03.stl" />
    <mesh file="arm/z1_Link04.stl" />
    <mesh file="arm/z1_Link05.stl" />
    <mesh file="arm/z1_Link06.stl" />
"""
        go2_content = go2_content.replace("  </asset>", asset_insert + "  </asset>")

    # Aggiungi default class z1 (nomi unici: z1_visual, z1_collision per evitare conflitto con go2)
    if 'default class="z1"' not in go2_content:
        z1_default = """
    <default class="z1">
      <joint damping="1" frictionloss="1"/>
      <general biastype="affine" gainprm="1000" biasprm="0 -1000 -100" forcerange="-30 30"/>
      <default class="z1_visual">
        <geom type="mesh" group="2" contype="0" conaffinity="0"/>
      </default>
      <default class="z1_collision">
        <geom type="cylinder" group="3" mass="0" density="0"/>
      </default>
    </default>
"""
        go2_content = go2_content.replace("    </default>\n  </default>", "    </default>\n" + z1_default + "  </default>")

    # Aggiungi attuatori braccio
    arm_actuators = """
    <general class="z1" name="arm_motor1" joint="arm_joint1" ctrlrange="-2.61799 2.61799"/>
    <general class="z1" name="arm_motor2" joint="arm_joint2" ctrlrange="0 2.96706" forcerange="-60 60" gainprm="1500" biasprm="0 -1500 -150"/>
    <general class="z1" name="arm_motor3" joint="arm_joint3" ctrlrange="-2.87979 0"/>
    <general class="z1" name="arm_motor4" joint="arm_joint4" ctrlrange="-1.51844 1.51844"/>
    <general class="z1" name="arm_motor5" joint="arm_joint5" ctrlrange="-1.3439 1.3439"/>
    <general class="z1" name="arm_motor6" joint="arm_joint6" ctrlrange="-2.79253 2.79253"/>
"""
    go2_content = go2_content.replace("  </actuator>", arm_actuators + "  </actuator>")

    # Aggiorna modello e meshdir
    go2_content = go2_content.replace('model="go2"', 'model="go2_d1"')
    go2_content = go2_content.replace('meshdir="assets"', 'meshdir="assets"')

    # Scrivi go2_d1.xml
    go2_d1_xml = os.path.join(GO2_D1_DIR, "go2_d1.xml")
    with open(go2_d1_xml, "w") as f:
        f.write(go2_content)

    # 4. Crea scene.xml
    scene_content = '''<mujoco model="go2_d1 scene">
  <include file="go2_d1.xml"/>

  <statistic center="0 0 0.1" extent="0.8"/>

  <visual>
    <headlight diffuse="0.6 0.6 0.6" ambient="0.3 0.3 0.3" specular="0 0 0"/>
    <rgba haze="0.15 0.25 0.35 1"/>
    <global azimuth="-130" elevation="-20"/>
  </visual>

  <asset>
    <texture type="skybox" builtin="gradient" rgb1="0.3 0.5 0.7" rgb2="0 0 0" width="512" height="3072"/>
    <texture type="2d" name="groundplane" builtin="checker" mark="edge" rgb1="0.2 0.3 0.4" rgb2="0.1 0.2 0.3"
      markrgb="0.8 0.8 0.8" width="300" height="300"/>
    <material name="groundplane" texture="groundplane" texuniform="true" texrepeat="5 5" reflectance="0.2"/>
  </asset>

  <worldbody>
    <light pos="0 0 1.5" dir="0 0 -1" directional="true"/>
    <geom name="floor" size="0 0 0.05" type="plane" material="groundplane"/>
  </worldbody>
</mujoco>
'''
    scene_path = os.path.join(GO2_D1_DIR, "scene.xml")
    with open(scene_path, "w") as f:
        f.write(scene_content)

    print("Modello creato:", go2_d1_xml)
    print("Scene:", scene_path)
    print("\nPer testare:")
    print("  cd unitree_mujoco/simulate_python")
    print("  # Modifica config: ROBOT_SCENE = '../unitree_robots/go2_d1/scene.xml'")
    print("  python3 unitree_mujoco.py")


if __name__ == "__main__":
    main()
