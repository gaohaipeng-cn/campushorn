<?php
/**
 * 校园网小喇叭 - HTTP 中转接口
 * 用于跨 VLAN/子网的 Sender 和 Receiver 通信
 *
 * 部署位置: /api/broadcast.php
 * 数据存储: /api/messages.json
 */

// ========================= 配置 =========================
define('AUTH_KEY', 'campus_broadcast_2024');  // 通信密钥，请修改为复杂密码
define('DATA_FILE', __DIR__ . '/messages.json');
define('CLASSES_FILE', __DIR__ . '/classes.json');
define('MAX_MESSAGE_AGE', 86400);  // 消息最大保留时间（秒），24小时
define('CLASS_TIMEOUT', 15);       // 班级心跳超时（秒），超过此时间视为离线

// ========================= 跨域支持 =========================
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, X-Auth-Key');
header('Content-Type: application/json; charset=utf-8');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(200);
    exit;
}

// ========================= 工具函数 =========================
function json_response($data, $code = 200) {
    http_response_code($code);
    echo json_encode($data, JSON_UNESCAPED_UNICODE);
    exit;
}

function auth_check() {
    $key = $_SERVER['HTTP_X_AUTH_KEY'] ?? '';
    if ($key !== AUTH_KEY) {
        json_response(['error' => 'Unauthorized'], 401);
    }
}

function load_messages() {
    if (!file_exists(DATA_FILE)) {
        return ['messages' => [], 'last_id' => 0];
    }
    $content = file_get_contents(DATA_FILE);
    if ($content === false || $content === '') {
        return ['messages' => [], 'last_id' => 0];
    }
    return json_decode($content, true) ?: ['messages' => [], 'last_id' => 0];
}

/**
 * 原子化读-改-写 messages.json（加文件锁，防止并发写入导致消息丢失）
 * $modifier 接收 &$data 引用，在此回调内修改数据后自动写入
 */
function save_messages_atomic($modifier) {
    $fp = fopen(DATA_FILE, 'c+');
    if (!$fp) return false;
    flock($fp, LOCK_EX);
    $raw = stream_get_contents($fp);
    $data = $raw ? (json_decode($raw, true) ?: ['messages' => [], 'last_id' => 0])
                 : ['messages' => [], 'last_id' => 0];
    $modifier($data);
    ftruncate($fp, 0);
    rewind($fp);
    fwrite($fp, json_encode($data, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
    fflush($fp);
    flock($fp, LOCK_UN);
    fclose($fp);
    return $data;
}

function clean_old_messages(&$data) {
    $now = time();
    $data['messages'] = array_filter($data['messages'], function($msg) use ($now) {
        $age = $now - ($msg['timestamp'] ?? 0);
        return $age < MAX_MESSAGE_AGE && !($msg['acked'] ?? false);
    });
    $data['messages'] = array_values($data['messages']);
}

function load_classes() {
    if (!file_exists(CLASSES_FILE)) {
        return ['classes' => []];
    }
    $content = file_get_contents(CLASSES_FILE);
    if ($content === false || $content === '') {
        return ['classes' => []];
    }
    return json_decode($content, true) ?: ['classes' => []];
}

function save_classes($data) {
    $json = json_encode($data, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
    file_put_contents(CLASSES_FILE, $json);
}

// ========================= 请求处理 =========================
$method = $_SERVER['REQUEST_METHOD'];

try {
    if ($method === 'GET') {
        // ============== Sender 查询在线班级列表 ==============
        if (($_GET['action'] ?? '') === 'classes') {
            $data = load_classes();
            $now = time();
            $classes = [];
            foreach ($data['classes'] as $ip => $cls) {
                if ($now - ($cls['last_heartbeat'] ?? 0) < CLASS_TIMEOUT) {
                    $classes[] = [
                        'ip' => $ip,
                        'class_name' => $cls['class_name'],
                    ];
                }
            }
            json_response(['success' => true, 'classes' => $classes]);
        }

        // ============== Receiver 获取新消息 ==============
        $target_ip = $_GET['ip'] ?? '';
        $last_id = intval($_GET['last_id'] ?? 0);

        if (empty($target_ip)) {
            json_response(['error' => 'Missing ip parameter'], 400);
        }

        $data = load_messages();
        clean_old_messages($data);

        // 筛选目标 IP 且 ID > last_id 且未确认的消息
        $new_messages = [];
        foreach ($data['messages'] as $msg) {
            if ($msg['target_ip'] === $target_ip && $msg['id'] > $last_id) {
                $new_messages[] = [
                    'id' => $msg['id'],
                    'teacher' => $msg['teacher'],
                    'content' => $msg['content'],
                    'duration' => $msg['duration'] ?? 60,
                    'sent_at' => $msg['timestamp']
                ];
            }
        }

        $max_id = $data['last_id'];
        foreach ($data['messages'] as $msg) {
            if ($msg['target_ip'] === $target_ip) {
                $max_id = max($max_id, $msg['id']);
            }
        }

        json_response([
            'success' => true,
            'messages' => $new_messages,
            'max_id' => $max_id
        ]);

    } elseif ($method === 'POST') {
        // ============== Sender 发送消息 或 Receiver 确认已读 ==============
        $input = json_decode(file_get_contents('php://input'), true);

        // 部分请求需要认证（发送消息）
        $action = $input['action'] ?? 'send';

        if ($action === 'register') {
            // ============== Receiver 注册班级（心跳）==============
            $class_name = $input['class_name'] ?? '';
            $ip = $input['ip'] ?? '';

            if (empty($ip) || empty($class_name)) {
                json_response(['error' => 'Missing ip or class_name'], 400);
            }

            $data = load_classes();
            $data['classes'][$ip] = [
                'class_name' => $class_name,
                'ip' => $ip,
                'last_heartbeat' => time()
            ];

            // 清理过期超过 5 倍超时的残留记录（防止文件膨胀）
            $now = time();
            $data['classes'] = array_filter($data['classes'], function($cls) use ($now) {
                return $now - ($cls['last_heartbeat'] ?? 0) < CLASS_TIMEOUT * 5;
            });

            save_classes($data);
            json_response(['success' => true, 'info' => 'Registered']);

        } elseif ($action === 'send') {
            auth_check();

            // 发送新消息
            $class_name = $input['class_name'] ?? '';
            $target_ip = $input['target_ip'] ?? '';
            $teacher = $input['teacher'] ?? '';
            $content = $input['content'] ?? '';
            $duration = intval($input['duration'] ?? 60);

            if (empty($target_ip) || empty($content)) {
                json_response(['error' => 'Missing required fields'], 400);
            }

            // 检查目标接收端是否在线
            // 策略：目标 IP 在 classes.json 中存在 且 心跳超时 → 拦截（防止幽灵通知）
            //       目标 IP 不在 classes.json 中 → 放行（可能是新上线/刚注册，防止误杀）
            $classes_data = load_classes();
            if (isset($classes_data['classes'][$target_ip])) {
                $now = time();
                $last_hb = $classes_data['classes'][$target_ip]['last_heartbeat'] ?? 0;
                if ($now - $last_hb >= CLASS_TIMEOUT) {
                    json_response([
                        'error' => 'Target offline',
                        'info' => ($class_name ?: $target_ip) . ' 当前不在线，通知未发送'
                    ], 410);
                }
            }

            $result = save_messages_atomic(function(&$data) use ($class_name, $target_ip, $teacher, $content, $duration) {
                clean_old_messages($data);

                $data['last_id']++;
                $new_id = $data['last_id'];

                $data['messages'][] = [
                    'id' => $new_id,
                    'class_name' => $class_name,
                    'target_ip' => $target_ip,
                    'teacher' => $teacher,
                    'content' => $content,
                    'duration' => $duration,
                    'timestamp' => time(),
                    'acked' => false
                ];
                // 通过引用返回 new_id
                $data['_new_id'] = $new_id;
            });

            $new_id = $result['_new_id'] ?? 0;

            json_response([
                'success' => true,
                'message_id' => $new_id,
                'info' => 'Message queued for ' . $target_ip
            ]);

        } elseif ($action === 'ack') {
            // 确认消息已读
            $msg_id = intval($input['id'] ?? 0);

            if ($msg_id <= 0) {
                json_response(['error' => 'Invalid message id'], 400);
            }

            save_messages_atomic(function(&$data) use ($msg_id) {
                foreach ($data['messages'] as &$msg) {
                    if ($msg['id'] === $msg_id) {
                        $msg['acked'] = true;
                        break;
                    }
                }
                unset($msg);
            });

            json_response(['success' => true, 'id' => $msg_id]);

        } else {
            json_response(['error' => 'Unknown action'], 400);
        }

    } else {
        json_response(['error' => 'Method not allowed'], 405);
    }

} catch (Exception $e) {
    json_response(['error' => 'Server error: ' . $e->getMessage()], 500);
}
