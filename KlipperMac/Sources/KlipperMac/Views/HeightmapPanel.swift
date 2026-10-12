import SwiftUI
import SceneKit
import simd

/// 3D bed-mesh surface rendered with SceneKit. Reads `bed_mesh.mesh_matrix`
/// from the live WebSocket status; shows a placeholder until a mesh exists.
struct HeightmapPanel: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Panel("Heightmap", icon: "cube.fill", tint: .pink, expands: true, plain: true) {
            if let stats = BedMesh.stats(from: model.live.raw) {
                HStack(spacing: 10) {
                    statChip("min", stats.min)
                    statChip("avg", stats.avg)
                    statChip("max", stats.max)
                }
            }
        } content: {
            if let grid = BedMesh.matrix(from: model.live.raw) {
                HeightmapScene(matrix: grid)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            } else {
                VStack(spacing: 8) {
                    Image(systemName: "cube")
                        .font(.system(size: 34)).foregroundStyle(.secondary)
                    Text("No bed mesh yet.")
                        .font(.callout).foregroundStyle(.secondary)
                    Text("Run BED_MESH_CALIBRATE (or load a profile) on a connected printer to populate this view.")
                        .font(.caption).foregroundStyle(.tertiary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private func statChip(_ label: String, _ v: Double) -> some View {
        HStack(spacing: 3) {
            Text(label).foregroundStyle(.secondary)
            Text(String(format: "%+.3f", v))
                .font(.system(.caption, design: .monospaced))
        }
        .padding(.horizontal, 7).padding(.vertical, 3)
        .background(Capsule().fill(Color(nsColor: .controlBackgroundColor)))
    }
}

enum BedMesh {
    /// Returns rows×cols z-matrix from bed_mesh status, if present.
    static func matrix(from raw: [String: [String: JSONValue]]) -> [[Double]]? {
        guard let rows = raw["bed_mesh"]?["mesh_matrix"]?.arrayValue else { return nil }
        let grid = rows.compactMap { row -> [Double]? in
            row.arrayValue?.compactMap { $0.doubleValue }
        }
        return grid.count >= 2 && grid.allSatisfy({ $0.count >= 2 }) ? grid : nil
    }

    static func stats(from raw: [String: [String: JSONValue]])
        -> (min: Double, max: Double, avg: Double)? {
        guard let flat = matrix(from: raw)?.flatMap({ $0 }), !flat.isEmpty
        else { return nil }
        return (flat.min() ?? 0, flat.max() ?? 0, flat.reduce(0, +) / Double(flat.count))
    }
}

struct HeightmapScene: NSViewRepresentable {
    let matrix: [[Double]]

    func makeNSView(context: Context) -> SCNView {
        let v = SCNView()
        v.allowsCameraControl = true
        v.autoenablesDefaultLighting = false
        v.backgroundColor = NSColor(calibratedWhite: 0.12, alpha: 1)
        v.scene = buildScene()
        v.antialiasingMode = .multisampling4X
        return v
    }

    func updateNSView(_ v: SCNView, context: Context) {
        v.scene = buildScene()
    }

    private func buildScene() -> SCNScene {
        let scene = SCNScene()
        let rows = matrix.count
        let cols = matrix[0].count
        var verts: [SCNVector3] = []
        var colors: [SIMD4<Float>] = []
        let zFlat = matrix.flatMap { $0 }
        let zMin = zFlat.min() ?? 0, zMax = zFlat.max() ?? 0
        let zSpan = max(zMax - zMin, 0.001)
        // exaggerate Z so mesh is visible; normalize XY to ~[-1,1]
        let nx = Double(cols - 1), ny = Double(rows - 1)
        for r in 0..<rows {
            for c in 0..<cols {
                let x = (Double(c) / nx - 0.5) * 2
                let y = (Double(r) / ny - 0.5) * 2
                let z = (matrix[r][c] - zMin) / zSpan
                verts.append(SCNVector3(Float(x), Float(z) * 0.6, Float(-y)))
                colors.append(Self.ramp(Float(z)))
            }
        }
        var idx: [Int32] = []
        for r in 0..<(rows - 1) {
            for c in 0..<(cols - 1) {
                let a = Int32(r * cols + c), b = a + 1
                let d = Int32((r + 1) * cols + c), e = d + 1
                idx += [a, d, b, b, d, e]
            }
        }
        let vData = verts.withUnsafeBytes { Data($0) }
        let cData = colors.withUnsafeBytes { Data($0) }
        let iData = idx.withUnsafeBytes { Data($0) }
        let vSrc = SCNGeometrySource(data: vData, semantic: .vertex,
                                     vectorCount: verts.count,
                                     usesFloatComponents: true,
                                     componentsPerVector: 3,
                                     bytesPerComponent: MemoryLayout<Float>.size,
                                     dataOffset: 0,
                                     dataStride: MemoryLayout<SCNVector3>.stride)
        let cSrc = SCNGeometrySource(data: cData, semantic: .color,
                                     vectorCount: colors.count,
                                     usesFloatComponents: true,
                                     componentsPerVector: 4,
                                     bytesPerComponent: MemoryLayout<Float>.size,
                                     dataOffset: 0,
                                     dataStride: MemoryLayout<SIMD4<Float>>.stride)
        let elem = SCNGeometryElement(data: iData, primitiveType: .triangles,
                                      primitiveCount: idx.count / 3,
                                      bytesPerIndex: MemoryLayout<Int32>.size)
        let geo = SCNGeometry(sources: [vSrc, cSrc], elements: [elem])
        let mat = SCNMaterial()
        mat.lightingModel = .constant
        mat.isDoubleSided = true
        geo.firstMaterial = mat
        scene.rootNode.addChildNode(SCNNode(geometry: geo))

        let cam = SCNNode()
        cam.camera = SCNCamera()
        cam.position = SCNVector3(1.6, 1.4, 2.0)
        cam.look(at: SCNVector3(0, 0, 0))
        scene.rootNode.addChildNode(cam)

        let light = SCNNode()
        light.light = SCNLight(); light.light?.type = .omni
        light.position = SCNVector3(2, 4, 3)
        scene.rootNode.addChildNode(light)
        return scene
    }

    /// blue → cyan → green → yellow → red
    static func ramp(_ t: Float) -> SIMD4<Float> {
        let stops: [(Float, SIMD3<Float>)] = [
            (0.0, [0.1, 0.3, 0.9]), (0.35, [0.1, 0.8, 0.8]),
            (0.55, [0.2, 0.85, 0.25]), (0.75, [0.95, 0.85, 0.1]),
            (1.0, [0.95, 0.2, 0.15]),
        ]
        let x = min(max(t, 0), 1)
        for i in 0..<(stops.count - 1) {
            if x <= stops[i + 1].0 {
                let f = (x - stops[i].0) / (stops[i + 1].0 - stops[i].0)
                let a = stops[i].1, b = stops[i + 1].1
                return SIMD4(a + (b - a) * f, 1)
            }
        }
        return SIMD4(stops.last!.1, 1)
    }
}
