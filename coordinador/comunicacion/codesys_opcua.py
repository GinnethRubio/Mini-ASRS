"""Cliente compartido: descubre namespace y conserva los tipos de escritura."""
from numbers import Real
import math
from opcua import Client, ua
from config import OPC_URL, OPC_TIMEOUT, OPC_NODES

class CodesysOPCUA:
    def __init__(self, url=OPC_URL, timeout=OPC_TIMEOUT):
        self.client = Client(url, timeout=timeout)
        self.nodes = {}
        self.types = {}
        self.conectado = False

    def conectar(self):
        try:
            self.client.connect()
            self.conectado = True
            # El mismo identificador simbólico, con el namespace del servidor.
            namespaces = self.client.get_namespace_array()
            for nombre, preferido in OPC_NODES.items():
                identificador = preferido.split(';s=', 1)[1]
                encontrado = None
                for ns in dict.fromkeys([4] + list(range(len(namespaces)))):
                    try:
                        node = self.client.get_node(f'ns={ns};s={identificador}')
                        node.get_value()
                        encontrado = node
                        break
                    except ua.UaStatusCodeError:
                        continue
                if encontrado is None:
                    raise RuntimeError(f'Símbolo ausente: {identificador}. Compile y descargue el proyecto integrado con su configuración de símbolos.')
                self.nodes[nombre] = encontrado
                self.types[nombre] = encontrado.get_data_type_as_variant_type()
            return self
        except Exception:
            self.desconectar()
            raise

    def leer(self, nombre):
        return self.nodes[nombre].get_value()

    def leer_bool(self, nombre):
        valor = self.leer(nombre)
        if not isinstance(valor, bool):
            raise TypeError(f'{nombre}: se esperaba BOOL, recibido {valor!r}')
        return valor

    def leer_string(self, nombre):
        valor = self.leer(nombre)
        if not isinstance(valor, str):
            raise TypeError(f'{nombre}: se esperaba STRING')
        return valor

    def escribir_bool(self, nombre, valor):
        if not isinstance(valor, bool):
            raise TypeError(nombre+' requiere bool')
        self.nodes[nombre].set_value(ua.Variant(valor, ua.VariantType.Boolean))

    def escribir_string(self, nombre, valor):
        limite = 20 if nombre == 'NUM_QR' else 80
        if not isinstance(valor, str) or len(valor) > limite:
            raise ValueError(f'{nombre}: cadena inválida o mayor de {limite}')
        self.nodes[nombre].set_value(ua.Variant(valor, ua.VariantType.String))

    def escribir_numero(self, nombre, valor):
        if isinstance(valor, bool) or not isinstance(valor, Real) or not math.isfinite(valor):
            raise ValueError(nombre+' requiere un número finito')
        tipo = self.types[nombre]
        enteros = (ua.VariantType.SByte, ua.VariantType.Byte, ua.VariantType.Int16,
                   ua.VariantType.UInt16, ua.VariantType.Int32, ua.VariantType.UInt32,
                   ua.VariantType.Int64, ua.VariantType.UInt64)
        if tipo in enteros:
            if int(valor) != valor:
                raise ValueError(nombre+' requiere un entero')
            valor = int(valor)
        elif tipo not in (ua.VariantType.Float, ua.VariantType.Double):
            raise TypeError(nombre+' no es numérico')
        self.nodes[nombre].set_value(ua.Variant(valor, tipo))

    def publicar_inventario(self, datos):
        # Las 112 posiciones se ordenan por columna/fila, no por orden del JSON.
        ocupadas, codigos = [False]*112, ['']*112
        indices = set()
        for c in datos['celdas']:
            col, fila = int(c['columna']), int(c['fila'])
            if not (1 <= col <= 4 and 1 <= fila <= 28):
                raise ValueError('Coordenada de inventario fuera de rango')
            i = (col-1)*28+fila-1
            if i in indices:
                raise ValueError('Coordenada de inventario duplicada')
            indices.add(i)
            ocupadas[i], codigos[i] = c['ocupada'], str(c.get('qr') or '')
        if len(indices) != 112:
            raise ValueError('Inventario incompleto')
        self.nodes['Inventario_Ocupada'].set_value(ua.Variant(ocupadas, ua.VariantType.Boolean))
        self.nodes['Inventario_Codigo'].set_value(ua.Variant(codigos, ua.VariantType.String))
        self.escribir_bool('Inventario_Listo', True)

    def actualizar_posicion_x(self, p): self.escribir_numero('EJE_X_POSICION', p)
    def actualizar_posicion_z(self, p): self.escribir_numero('EJE_Z_POSICION', p)
    def actualizar_estado_y(self, extendido):
        self.escribir_bool('EJE_Y_ESTADO', bool(extendido))
        self.escribir_bool('Y_ESTADO_VALIDO', True)
    def actualizar_estado_gripper(self, cerrado):
        self.escribir_bool('GRIPPER_ESTADO', bool(cerrado))
        self.escribir_bool('GRIPPER_ESTADO_VALIDO', True)

    def desconectar(self):
        if self.conectado:
            try: self.client.disconnect()
            finally:
                self.conectado = False
                self.nodes.clear()
