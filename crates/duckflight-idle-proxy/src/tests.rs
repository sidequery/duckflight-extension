use super::*;
use std::sync::atomic::{AtomicBool, Ordering};
use tokio::io::{AsyncRead, AsyncWrite};

struct TestState {
    deployment: Value,
    busy: bool,
    permit: Option<String>,
    scale_downs: usize,
    extra_pod: bool,
    unsupported: bool,
}

struct Fixture {
    state: Arc<Mutex<TestState>>,
    slow_sleep: Arc<AtomicBool>,
    sleep_started: Arc<tokio::sync::Notify>,
    release_sleep: Arc<tokio::sync::Notify>,
    api: SocketAddr,
    control: SocketAddr,
    certificate: Certificate,
    directory: tempfile::TempDir,
    tasks: JoinSet<()>,
}

async fn read_request<S: AsyncRead + Unpin>(stream: &mut S) -> Result<(String, Value)> {
    let mut header = Vec::new();
    while !header.ends_with(b"\r\n\r\n") {
        header.push(stream.read_u8().await?);
        if header.len() > 8192 {
            bail!("oversized test request")
        }
    }
    let header = String::from_utf8(header)?;
    let length = header
        .lines()
        .find_map(|line| {
            let (key, value) = line.split_once(':')?;
            key.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().unwrap())
        })
        .unwrap_or(0);
    let mut body = vec![0; length];
    stream.read_exact(&mut body).await?;
    Ok((
        header.lines().next().unwrap().to_string(),
        if body.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&body)?
        },
    ))
}

async fn respond<S: AsyncWrite + Unpin>(stream: &mut S, code: u32, body: Value) -> Result<()> {
    let body = body.to_string();
    stream.write_all(format!("HTTP/1.1 {code} Result\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).as_bytes()).await?;
    stream.shutdown().await?;
    Ok(())
}

impl Fixture {
    async fn new() -> Self {
        let state = Arc::new(Mutex::new(TestState {
            deployment: json!({"metadata":{"uid":"deployment-one","resourceVersion":"1","annotations":{}},"spec":{"replicas":0}}),
            busy: false,
            permit: None,
            scale_downs: 0,
            extra_pod: false,
            unsupported: false,
        }));
        let slow_sleep = Arc::new(AtomicBool::new(false));
        let sleep_started = Arc::new(tokio::sync::Notify::new());
        let release_sleep = Arc::new(tokio::sync::Notify::new());
        let api_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let api = api_listener.local_addr().unwrap();
        let control_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let control = control_listener.local_addr().unwrap();
        let rcgen::CertifiedKey { cert, key_pair } =
            rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        let certificate = Certificate::from_pem(cert.pem().as_bytes()).unwrap();
        let tls = tokio_rustls::rustls::ServerConfig::builder()
            .with_no_client_auth()
            .with_single_cert(
                vec![cert.der().clone()],
                tokio_rustls::rustls::pki_types::PrivateKeyDer::Pkcs8(
                    key_pair.serialize_der().into(),
                ),
            )
            .unwrap();
        let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(tls));
        let mut tasks = JoinSet::new();
        let (api_state, slow, started) = (state.clone(), slow_sleep.clone(), sleep_started.clone());
        let release = release_sleep.clone();
        tasks.spawn(async move {
            while let Ok((mut stream, _)) = api_listener.accept().await {
                let (state, slow, started) = (api_state.clone(), slow.clone(), started.clone());
                let release = release.clone();
                tokio::spawn(async move {
                    let (request, patch) = read_request(&mut stream).await.unwrap();
                    if request.starts_with("PATCH ") && patch["spec"]["replicas"] == 0 && slow.load(Ordering::SeqCst) {
                        started.notify_one();
                        release.notified().await;
                    }
                    let mut state = state.lock().await;
                    let (code, body) = if request.starts_with("PATCH ") {
                        if patch["metadata"]["resourceVersion"] != state.deployment["metadata"]["resourceVersion"] {
                            (409, json!({"message":"resourceVersion conflict"}))
                        } else {
                            let version: u32 = state.deployment["metadata"]["resourceVersion"].as_str().unwrap().parse().unwrap();
                            state.deployment["metadata"]["resourceVersion"] = json!((version + 1).to_string());
                            state.deployment["metadata"]["annotations"] = patch["metadata"]["annotations"].clone();
                            if let Some(count) = patch["spec"]["replicas"].as_u64() {
                                state.deployment["spec"]["replicas"] = json!(count);
                                if count == 0 { state.scale_downs += 1; state.permit = None; }
                            }
                            (200, state.deployment.clone())
                        }
                    } else if request.contains("/pods?") {
                        let mut pods = Vec::new();
                        if state.deployment["spec"]["replicas"] == 1 {
                            pods.push(json!({"metadata":{"uid":"worker-one"},"status":{"podIP":"127.0.0.1","conditions":[{"type":"Ready","status":"True"}]}}));
                            if state.extra_pod { pods.push(json!({"metadata":{"uid":"worker-two","deletionTimestamp":"now"},"status":{}})); }
                        }
                        (200, json!({"items":pods}))
                    } else { (200, state.deployment.clone()) };
                    drop(state);
                    let _ = respond(&mut stream, code, body).await;
                });
            }
        });
        let control_state = state.clone();
        tasks.spawn(async move {
            while let Ok((stream, _)) = control_listener.accept().await {
                let state = control_state.clone();
                let acceptor = acceptor.clone();
                tokio::spawn(async move {
                    let mut stream = acceptor.accept(stream).await.unwrap();
                    let (request, _) = read_request(&mut stream).await.unwrap();
                    let mut state = state.lock().await;
                    let (code, body) = if state.unsupported { (404, json!({})) }
                    else if let Some(permit) = request.strip_prefix("POST /resume/").and_then(|s| s.strip_suffix(" HTTP/1.1")) {
                        if state.permit.as_deref() == Some(permit) { state.permit = None; (200, json!({})) }
                        else { (409, json!({})) }
                    } else {
                        if request.starts_with("POST /suspend ") && !state.busy { state.permit = Some("permit-one".into()); }
                        (200, json!({"protocol_version":1,"boot_id":"boot-one","ready":true,"busy":state.busy,"permit":state.permit}))
                    };
                    drop(state);
                    let _ = respond(&mut stream, code, body).await;
                });
            }
        });
        let directory = tempfile::tempdir().unwrap();
        std::fs::write(directory.path().join("token"), "t".repeat(32)).unwrap();
        Self {
            state,
            slow_sleep,
            sleep_started,
            release_sleep,
            api,
            control,
            certificate,
            directory,
            tasks,
        }
    }

    async fn controller(&self) -> Controller {
        let config = Config {
            namespace: "test".into(),
            deployment: "worker".into(),
            selector: "worker=test".into(),
            hostname: "localhost".into(),
            control_port: self.control.port(),
            idle_timeout: Duration::from_millis(1),
            cold_start_timeout: Duration::from_secs(5),
            token_file: self.directory.path().join("token"),
            ca: self.certificate.clone(),
            listeners: vec![],
        };
        let kube = Kubernetes {
            client: Client::builder()
                .timeout(Duration::from_secs(2))
                .build()
                .unwrap(),
            base: format!("http://{}", self.api),
            token_file: config.token_file.clone(),
            deployment_path: "/deployments/worker".into(),
            pods_path: "/pods".into(),
            selector: config.selector.clone(),
        };
        let mut controller = Controller {
            config,
            kube,
            owner: uuid::Uuid::new_v4().to_string(),
            uid: "deployment-one".into(),
            needs_fence: true,
            connections: 0,
            idle_since: None,
        };
        controller.fence().await.unwrap();
        controller
    }

    fn expire(controller: &mut Controller) {
        controller.idle_since = Some((
            "worker-one".into(),
            "boot-one".into(),
            Instant::now() - Duration::from_secs(1),
        ));
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.tasks.abort_all();
    }
}

#[tokio::test]
async fn cold_wake_then_idle_sleep_and_busy_state_refusal() {
    let fixture = Fixture::new().await;
    let mut controller = fixture.controller().await;
    controller.wake().await.unwrap();
    assert_eq!(fixture.state.lock().await.deployment["spec"]["replicas"], 1);
    fixture.state.lock().await.busy = true; // Detached Flight session/query.
    Fixture::expire(&mut controller);
    controller.idle_tick().await.unwrap();
    assert!(controller.idle_since.is_none());
    assert_eq!(fixture.state.lock().await.scale_downs, 0);
    fixture.state.lock().await.busy = false;
    controller.idle_tick().await.unwrap();
    assert_eq!(fixture.state.lock().await.scale_downs, 0); // Fresh idle interval.
    Fixture::expire(&mut controller);
    controller.idle_tick().await.unwrap();
    assert_eq!(fixture.state.lock().await.scale_downs, 1);
    controller.wake().await.unwrap();
    assert_eq!(fixture.state.lock().await.deployment["spec"]["replicas"], 1);
}

#[tokio::test]
async fn missing_capability_or_overlapping_worker_never_scales_down() {
    let fixture = Fixture::new().await;
    let mut controller = fixture.controller().await;
    controller.wake().await.unwrap();
    fixture.state.lock().await.unsupported = true;
    Fixture::expire(&mut controller);
    assert!(controller.idle_tick().await.is_err());
    assert_eq!(fixture.state.lock().await.scale_downs, 0);
    fixture.state.lock().await.unsupported = false;
    fixture.state.lock().await.extra_pod = true;
    Fixture::expire(&mut controller);
    controller.idle_tick().await.unwrap();
    assert_eq!(fixture.state.lock().await.scale_downs, 0);
}

#[tokio::test]
async fn timed_out_sleep_is_fenced_before_worker_resume() {
    let fixture = Fixture::new().await;
    let mut controller = fixture.controller().await;
    controller.wake().await.unwrap();
    controller.kube.client = Client::builder().timeout(Duration::from_secs(1)).build().unwrap();
    fixture.slow_sleep.store(true, Ordering::SeqCst);
    Fixture::expire(&mut controller);
    assert!(controller.idle_tick().await.is_err());
    assert!(!controller.needs_fence);
    assert!(fixture.state.lock().await.permit.is_none());
    fixture.release_sleep.notify_one();
    sleep(Duration::from_millis(20)).await;
    let state = fixture.state.lock().await;
    assert_eq!(state.deployment["spec"]["replicas"], 1);
    assert_eq!(state.scale_downs, 0);
}

#[tokio::test]
async fn proxy_restart_fences_old_sleep_before_worker_resume() {
    let fixture = Fixture::new().await;
    let mut old = fixture.controller().await;
    old.wake().await.unwrap();
    fixture.slow_sleep.store(true, Ordering::SeqCst);
    Fixture::expire(&mut old);
    let sleeping = tokio::spawn(async move { old.idle_tick().await });
    fixture.sleep_started.notified().await;
    sleeping.abort(); // Crash after submitting sleep, before receiving its result.
    let mut replacement = fixture.controller().await;
    replacement.wake().await.unwrap();
    assert!(fixture.state.lock().await.permit.is_none());
    fixture.release_sleep.notify_one();
    sleep(Duration::from_millis(20)).await;
    let state = fixture.state.lock().await;
    assert_eq!(state.deployment["spec"]["replicas"], 1);
    assert_eq!(state.scale_downs, 0);
}

#[tokio::test]
async fn both_protocol_ports_pass_bytes_and_keep_open_sessions_awake() {
    let fixture = Fixture::new().await;
    let controller = Arc::new(Mutex::new(fixture.controller().await));
    for payload in [
        b"\0\0\0\x08\x04\xd2\x16\x2f".as_slice(),
        b"\x16\x03\x01flight-tls".as_slice(),
    ] {
        let upstream = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = upstream.local_addr().unwrap().port();
        let echo = tokio::spawn(async move {
            let (mut stream, _) = upstream.accept().await.unwrap();
            let (mut read, mut write) = stream.split();
            tokio::io::copy(&mut read, &mut write).await.unwrap();
        });
        let proxy = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let mut client = TcpStream::connect(proxy.local_addr().unwrap())
            .await
            .unwrap();
        let (stream, _) = proxy.accept().await.unwrap();
        let task_controller = controller.clone();
        let copying = tokio::spawn(proxy_connection(stream, port, task_controller));
        client.write_all(payload).await.unwrap();
        let mut reply = vec![0; payload.len()];
        client.read_exact(&mut reply).await.unwrap();
        assert_eq!(reply, payload);
        let mut state = controller.lock().await;
        Fixture::expire(&mut state);
        state.idle_tick().await.unwrap();
        assert_eq!(state.connections, 1);
        drop(state);
        client.write_all(b"still-open").await.unwrap();
        let mut reply = [0; 10];
        client.read_exact(&mut reply).await.unwrap();
        assert_eq!(&reply, b"still-open");
        drop(client);
        copying.await.unwrap().unwrap();
        echo.await.unwrap();
        assert_eq!(controller.lock().await.connections, 0);
    }
    assert_eq!(fixture.state.lock().await.scale_downs, 0);
}
