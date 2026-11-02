import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rutas  # noqa: E402,F401
from estrategias_colocacion import costo_tiempo, elegir_celda, ordenar_celdas, destino_compatible  # noqa: E402
from control_portico import ControlPortico, ARCHIVO_COORDENADAS


class EstrategiasTest(unittest.TestCase):
    def test_tiempo_puede_superar_distancia_con_ejes_distintos(self):
        c={1:dict(x=-.062,z_entrada=-.675,z_salida=-.675),
           2:dict(x=-.262,z_entrada=-.375,z_salida=-.375)}
        self.assertEqual(elegir_celda(c,1,c),1)
        self.assertEqual(elegir_celda(c,2,c,vx=.05,vz=.5,ax=.1,az=1),2)

    def test_rack_lleno_y_ocupadas_excluidas(self):
        c={1:dict(x=-.262,z_entrada=-.675,z_salida=-.675),
           2:dict(x=.1,z_entrada=0,z_salida=-.015)}
        for estrategia in (0,1,2):
            self.assertIsNone(elegir_celda([],estrategia,c))
            self.assertEqual(elegir_celda([2],estrategia,c),2)

    def test_desempate_y_rechazo_de_datos_invalidos(self):
        c={i:dict(x=-.368,z_entrada=-.528,z_salida=-.543) for i in (2,1)}
        self.assertEqual(elegir_celda([2,1],2,c),1)
        with self.assertRaises(ValueError):elegir_celda(c,3,c)
        with self.assertRaises(ValueError):costo_tiempo(c[1],vx=0)
        with self.assertRaises(ValueError):costo_tiempo(dict(x=float('nan'),z_entrada=0,z_salida=0))

    def test_112_celdas_optimo_para_cada_prefijo_y_total_invariante(self):
        coords=ControlPortico._cargar_coordenadas(ARCHIVO_COORDENADAS)
        c={(col-1)*28+fila:v for (col,fila),v in coords.items()}
        orden=list(ordenar_celdas(c,2))
        self.assertEqual(len(set(orden)),112)
        self.assertEqual(orden[:4],[28,56,27,55])
        costos=[costo_tiempo(c[i]) for i in orden]
        for n in (1,28,56,112):
            self.assertAlmostEqual(sum(costos[:n]),sum(sorted(costos)[:n]),places=5)
        self.assertAlmostEqual(sum(costos),sum(costo_tiempo(v) for v in c.values()),places=8)


    def test_destino_del_plc_acepta_empates_y_rechaza_celdas_peores(self):
        coords=ControlPortico._cargar_coordenadas(ARCHIVO_COORDENADAS)
        c={(col-1)*28+fila:v for (col,fila),v in coords.items()}
        # 35 y 67 empatan en el modelo; en REAL de 32 bits el PLC puede preferir 67.
        self.assertAlmostEqual(costo_tiempo(c[35]),costo_tiempo(c[67]),places=9)
        self.assertTrue(destino_compatible({35,67},67,c))
        self.assertTrue(destino_compatible({35,67},35,c))
        libres=set(c)
        optimo=elegir_celda(libres,2,c)
        peor=max(libres,key=lambda i:costo_tiempo(c[i]))
        self.assertTrue(destino_compatible(libres,optimo,c))
        self.assertFalse(destino_compatible(libres,peor,c))
        self.assertFalse(destino_compatible(libres-{optimo},optimo,c))

if __name__=='__main__':unittest.main(verbosity=2)
