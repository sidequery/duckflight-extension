//! A single-owner, TLS-pass-through wake proxy for PostgreSQL and Flight SQL.
//! Sleep requires a core-issued admission fence, not merely zero TCP sockets.
use anyhow::{Context, Result, bail};
use reqwest::{Certificate, Client, Method};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{env, net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::Mutex,
    task::JoinSet,
    time::{Instant, sleep, timeout},
};

const OWNER_ANNOTATION: &str = "duckflight.sidequery.dev/idle-owner";
const OPERATION_ANNOTATION: &str = "duckflight.sidequery.dev/idle-operation";

struct Config {
    namespace: String,
    deployment: String,
    selector: String,
    hostname: String,
    control_port: u16,
    idle_timeout: Duration,
    cold_start_timeout: Duration,
    token_file: PathBuf,
    ca: Certificate,
    listeners: Vec<u16>,
}

impl Config {
    fn from_env() -> Result<Self> {
        let mut listeners = Vec::new();
        for name in [
            "DUCKFLIGHT_PROXY_POSTGRES_PORT",
            "DUCKFLIGHT_PROXY_FLIGHT_PORT",
        ] {
            if let Ok(value) = env::var(name) {
                listeners.push(value.parse()?);
            }
        }
        if listeners.is_empty() {
            bail!("enable at least one proxy listener")
        }
        Ok(Self {
            namespace: env::var("POD_NAMESPACE")?,
            deployment: env::var("DUCKFLIGHT_PROXY_DEPLOYMENT")?,
            selector: env::var("DUCKFLIGHT_PROXY_SELECTOR")?,
            hostname: env::var("DUCKFLIGHT_PROXY_HOSTNAME")?,
            control_port: env::var("DUCKFLIGHT_PROXY_CONTROL_PORT")?.parse()?,
            idle_timeout: Duration::from_secs(
                env::var("DUCKFLIGHT_PROXY_IDLE_TIMEOUT_SECONDS")?.parse()?,
            ),
            cold_start_timeout: Duration::from_secs(
                env::var("DUCKFLIGHT_PROXY_COLD_START_TIMEOUT_SECONDS")?.parse()?,
            ),
            token_file: env::var("DUCKFLIGHT_PROXY_TOKEN_FILE")?.into(),
            ca: Certificate::from_pem(&std::fs::read(env::var("DUCKFLIGHT_PROXY_CA_FILE")?)?)?,
            listeners,
        })
    }
}

struct Kubernetes {
    client: Client,
    base: String,
    token_file: PathBuf,
    deployment_path: String,
    pods_path: String,
    selector: String,
}

impl Kubernetes {
    fn new(config: &Config) -> Result<Self> {
        let directory = PathBuf::from("/var/run/secrets/kubernetes.io/serviceaccount");
        let ca = Certificate::from_pem(&std::fs::read(directory.join("ca.crt"))?)?;
        let host = env::var("KUBERNETES_SERVICE_HOST")?;
        let port = env::var("KUBERNETES_SERVICE_PORT_HTTPS").unwrap_or_else(|_| "443".into());
        let host = if host.contains(':') {
            format!("[{host}]")
        } else {
            host
        };
        Ok(Self {
            client: Client::builder()
                .add_root_certificate(ca)
                .timeout(Duration::from_secs(15))
                .build()?,
            base: format!("https://{host}:{port}"),
            token_file: directory.join("token"),
            deployment_path: format!(
                "/apis/apps/v1/namespaces/{}/deployments/{}",
                config.namespace, config.deployment
            ),
            pods_path: format!("/api/v1/namespaces/{}/pods", config.namespace),
            selector: config.selector.clone(),
        })
    }

    async fn deployment(&self) -> Result<Value> {
        Ok(self
            .client
            .get(format!("{}{}", self.base, self.deployment_path))
            .bearer_auth(std::fs::read_to_string(&self.token_file)?.trim())
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?)
    }

    async fn patch(
        &self,
        current: &Value,
        owner: &str,
        operation: &str,
        replicas: Option<u32>,
    ) -> Result<Value> {
        let mut patch = json!({"metadata": {
            "resourceVersion": current["metadata"]["resourceVersion"],
            "uid": current["metadata"]["uid"],
            "annotations": {OWNER_ANNOTATION: owner, OPERATION_ANNOTATION: operation}
        }});
        if let Some(replicas) = replicas {
            patch["spec"] = json!({"replicas": replicas});
        }
        Ok(self
            .client
            .patch(format!("{}{}", self.base, self.deployment_path))
            .header("Content-Type", "application/merge-patch+json")
            .bearer_auth(std::fs::read_to_string(&self.token_file)?.trim())
            .json(&patch)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?)
    }

    async fn pods(&self) -> Result<Vec<Value>> {
        let result: Value = self
            .client
            .get(format!("{}{}", self.base, self.pods_path))
            .query(&[("labelSelector", &self.selector)])
            .bearer_auth(std::fs::read_to_string(&self.token_file)?.trim())
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(result["items"]
            .as_array()
            .context("missing pod list")?
            .clone())
    }
}

#[derive(Clone)]
struct Backend {
    uid: String,
    ip: std::net::IpAddr,
    control: Client,
    url: String,
}

#[derive(Debug, Deserialize)]
struct IdleStatus {
    protocol_version: u32,
    boot_id: String,
    ready: bool,
    busy: bool,
    permit: Option<String>,
}

impl Backend {
    fn from_pod(pod: &Value, config: &Config) -> Result<Self> {
        let ip = pod["status"]["podIP"]
            .as_str()
            .context("missing pod IP")?
            .parse()?;
        Ok(Self {
            uid: pod["metadata"]["uid"]
                .as_str()
                .context("missing pod UID")?
                .to_string(),
            ip,
            control: Client::builder()
                .add_root_certificate(config.ca.clone())
                .https_only(true)
                .no_proxy()
                .resolve(&config.hostname, SocketAddr::new(ip, config.control_port))
                .timeout(Duration::from_secs(10))
                .build()?,
            url: format!("https://{}:{}", config.hostname, config.control_port),
        })
    }

    async fn request(
        &self,
        method: Method,
        path: &str,
        config: &Config,
    ) -> Result<reqwest::Response> {
        Ok(self
            .control
            .request(method, format!("{}{path}", self.url))
            .bearer_auth(std::fs::read_to_string(&config.token_file)?.trim())
            .send()
            .await?
            .error_for_status()?)
    }

    async fn status(&self, suspend: bool, config: &Config) -> Result<IdleStatus> {
        let status: IdleStatus = self
            .request(
                if suspend { Method::POST } else { Method::GET },
                if suspend { "/suspend" } else { "/status" },
                config,
            )
            .await?
            .json()
            .await?;
        if status.protocol_version != 1 || status.boot_id.is_empty() {
            bail!("unsupported idle-control capability")
        }
        Ok(status)
    }

    async fn resume(&self, permit: &str, config: &Config) -> Result<()> {
        self.request(Method::POST, &format!("/resume/{permit}"), config)
            .await?;
        Ok(())
    }
}

fn ready(pod: &Value) -> bool {
    pod["metadata"]["deletionTimestamp"].is_null()
        && pod["status"]["conditions"]
            .as_array()
            .is_some_and(|conditions| {
                conditions
                    .iter()
                    .any(|condition| condition["type"] == "Ready" && condition["status"] == "True")
            })
}

struct Controller {
    config: Config,
    kube: Kubernetes,
    owner: String,
    uid: String,
    // A timed-out write may still be in flight. A resourceVersion-changing
    // fence must succeed before any suspended worker is resumed or reused.
    needs_fence: bool,
    connections: usize,
    idle_since: Option<(String, String, Instant)>,
}

impl Controller {
    async fn new(config: Config) -> Result<Self> {
        let kube = Kubernetes::new(&config)?;
        let current = kube.deployment().await?;
        let uid = current["metadata"]["uid"]
            .as_str()
            .context("missing deployment UID")?
            .to_string();
        let mut controller = Self {
            config,
            kube,
            owner: uuid::Uuid::new_v4().to_string(),
            uid,
            needs_fence: true,
            connections: 0,
            idle_since: None,
        };
        controller.fence().await?;
        Ok(controller)
    }

    async fn current(&self) -> Result<Value> {
        let current = self.kube.deployment().await?;
        if current["metadata"]["uid"] != self.uid {
            bail!("worker Deployment was replaced; restart proxy")
        }
        if !self.needs_fence && current["metadata"]["annotations"][OWNER_ANNOTATION] != self.owner {
            bail!("another controller owns this worker Deployment; restart proxy")
        }
        Ok(current)
    }

    async fn fence(&mut self) -> Result<()> {
        let current = self.current().await?;
        self.needs_fence = true;
        self.kube
            .patch(
                &current,
                &self.owner,
                &uuid::Uuid::new_v4().to_string(),
                None,
            )
            .await?;
        self.needs_fence = false;
        Ok(())
    }

    async fn scale(&mut self, current: &Value, replicas: u32) -> Result<()> {
        self.needs_fence = true;
        self.kube
            .patch(
                current,
                &self.owner,
                &uuid::Uuid::new_v4().to_string(),
                Some(replicas),
            )
            .await?;
        self.needs_fence = false;
        Ok(())
    }

    async fn ready_backend(&self) -> Result<Option<Backend>> {
        let pods = self.kube.pods().await?;
        // A rolling upgrade can overlap workers. Never let one idle replacement
        // justify scaling down an old pod that may still own sessions.
        if pods.len() != 1 {
            return Ok(None);
        }
        let ready_pods: Vec<_> = pods.iter().filter(|pod| ready(pod)).collect();
        if ready_pods.len() > 1 {
            bail!("idle mode requires exactly one worker")
        }
        ready_pods
            .first()
            .map(|pod| Backend::from_pod(pod, &self.config))
            .transpose()
    }

    async fn wake(&mut self) -> Result<Backend> {
        self.idle_since = None;
        let deadline = Instant::now() + self.config.cold_start_timeout;
        loop {
            if Instant::now() >= deadline {
                bail!("DuckFlight cold start timed out")
            }
            if self.needs_fence {
                self.fence().await?;
            }
            let current = self.current().await?;
            match current["spec"]["replicas"].as_u64() {
                Some(0) => {
                    // A deleting pod may still accept sockets. Never reuse it
                    // or wake a replacement until all old pods have gone.
                    if self.kube.pods().await?.is_empty() {
                        self.scale(&current, 1).await?;
                    }
                }
                Some(1) => {
                    if let Some(backend) = self.ready_backend().await? {
                        let status = backend.status(false, &self.config).await?;
                        if let Some(permit) = status.permit {
                            backend.resume(&permit, &self.config).await?;
                        }
                        return Ok(backend);
                    }
                }
                _ => bail!("idle mode requires worker replicas to be zero or one"),
            }
            sleep(Duration::from_millis(500)).await;
        }
    }

    async fn idle_tick(&mut self) -> Result<()> {
        if self.needs_fence {
            self.fence().await?;
        }
        if self.connections > 0 {
            self.idle_since = None;
            return Ok(());
        }
        let current = self.current().await?;
        if current["spec"]["replicas"] == 0 {
            self.idle_since = None;
            return Ok(());
        }
        if current["spec"]["replicas"] != 1 {
            bail!("idle mode requires exactly one worker")
        }
        let Some(backend) = self.ready_backend().await? else {
            self.idle_since = None;
            return Ok(());
        };
        let status = backend.status(false, &self.config).await?;
        if let Some(permit) = status.permit {
            // Recovery follows the API fence above: a delayed old sleep write
            // cannot now kill this worker after it is resumed.
            backend.resume(&permit, &self.config).await?;
            self.idle_since = None;
            return Ok(());
        }
        if status.busy {
            self.idle_since = None;
            return Ok(());
        }
        let since = match &self.idle_since {
            Some((uid, boot, since)) if *uid == backend.uid && *boot == status.boot_id => *since,
            _ => {
                let since = Instant::now();
                self.idle_since = Some((backend.uid.clone(), status.boot_id.clone(), since));
                since
            }
        };
        if since.elapsed() < self.config.idle_timeout {
            return Ok(());
        }
        let suspended = backend.status(true, &self.config).await?;
        let Some(permit) = suspended.permit else {
            self.idle_since = None;
            return Ok(());
        };
        if suspended.busy || suspended.boot_id != status.boot_id {
            bail!("worker changed during idle suspension")
        }
        // The core gate remains closed through Kubernetes deletion. On any
        // ambiguous write outcome, do not resume until a new API fence wins.
        match self.scale(&current, 0).await {
            Ok(()) => eprintln!("DuckFlight worker suspended after idle timeout"),
            Err(error) => {
                self.fence().await?;
                let observed = self.current().await?;
                if observed["spec"]["replicas"] == 1 {
                    backend.resume(&permit, &self.config).await?;
                }
                return Err(error);
            }
        }
        self.idle_since = None;
        Ok(())
    }
}

async fn proxy_connection(
    mut client: TcpStream,
    port: u16,
    controller: Arc<Mutex<Controller>>,
) -> Result<()> {
    // TCP probes that send no protocol data never wake a worker. Established
    // protocol sockets have no idle/read timeout and remain pinned until close.
    let mut first = [0_u8; 1];
    if timeout(Duration::from_secs(10), client.read(&mut first)).await?? == 0 {
        return Ok(());
    }
    let backend = {
        let mut controller = controller.lock().await;
        controller.connections += 1;
        match controller.wake().await {
            Ok(backend) => backend,
            Err(error) => {
                controller.connections -= 1;
                return Err(error);
            }
        }
    };
    let result = async {
        let mut upstream = TcpStream::connect(SocketAddr::new(backend.ip, port)).await?;
        upstream.set_nodelay(true)?;
        client.set_nodelay(true)?;
        upstream.write_all(&first).await?;
        tokio::io::copy_bidirectional(&mut client, &mut upstream).await?;
        Ok(())
    }
    .await;
    let mut controller = controller.lock().await;
    controller.connections -= 1;
    controller.idle_since = None;
    result
}

async fn health(listener: TcpListener) -> Result<()> {
    loop {
        let (mut stream, _) = listener.accept().await?;
        tokio::spawn(async move {
            let _ = timeout(Duration::from_secs(2), async {
                let mut request = [0_u8; 1024];
                if stream.read(&mut request).await? == 0 {
                    return Ok(());
                }
                stream
                    .write_all(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok",
                    )
                    .await
            })
            .await;
        });
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    if env::args().nth(1).as_deref() == Some("worker-ready") {
        return worker_ready().await;
    }
    let config = Config::from_env()?;
    let mut listeners = Vec::new();
    for port in &config.listeners {
        listeners.push((*port, TcpListener::bind(("0.0.0.0", *port)).await?));
    }
    let controller = Arc::new(Mutex::new(Controller::new(config).await?));
    let health_listener = TcpListener::bind("0.0.0.0:4090").await?;
    let health_task = tokio::spawn(health(health_listener));
    let idle_controller = controller.clone();
    let idle_task = tokio::spawn(async move {
        loop {
            sleep(Duration::from_secs(5)).await;
            let mut controller = idle_controller.lock().await;
            if let Err(error) = controller.idle_tick().await {
                controller.idle_since = None;
                eprintln!("idle check refused: {error:#}");
            }
        }
    });
    let mut acceptors = JoinSet::new();
    let (accepted_tx, mut accepted_rx) = tokio::sync::mpsc::channel(128);
    for (port, listener) in listeners {
        let accepted_tx = accepted_tx.clone();
        acceptors.spawn(async move {
            loop {
                let (stream, _) = listener.accept().await?;
                if accepted_tx.send((port, stream)).await.is_err() {
                    return Ok::<_, anyhow::Error>(());
                }
            }
        });
    }
    drop(accepted_tx);
    let mut connections = JoinSet::new();
    let terminate = async {
        #[cfg(unix)]
        {
            let mut signal =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                    .expect("register SIGTERM");
            tokio::select! { _ = signal.recv() => {}, _ = tokio::signal::ctrl_c() => {} }
        }
        #[cfg(not(unix))]
        {
            let _ = tokio::signal::ctrl_c().await;
        }
    };
    tokio::pin!(terminate);
    loop {
        tokio::select! {
            _ = &mut terminate => break,
            Some(_) = connections.join_next(), if !connections.is_empty() => {},
            connection = accepted_rx.recv() => {
                let Some((port, stream)) = connection else { bail!("proxy listeners stopped") };
                let controller = controller.clone();
                connections.spawn(async move {
                    if let Err(error) = proxy_connection(stream, port, controller).await { eprintln!("connection failed: {error:#}"); }
                });
            }
        }
    }
    acceptors.abort_all();
    idle_task.abort();
    health_task.abort();
    // No proxy-imposed session timeout. Kubernetes' termination grace still
    // bounds an explicit proxy rollout/deletion, independently of idle sleep.
    while connections.join_next().await.is_some() {}
    Ok(())
}

async fn worker_ready() -> Result<()> {
    let address: SocketAddr = env::var("DUCKFLIGHT_IDLE_CONTROL_ADDRESS")?.parse()?;
    let hostname = env::var("DUCKFLIGHT_IDLE_SERVER_NAME")?;
    let client = Client::builder()
        .https_only(true)
        .no_proxy()
        .add_root_certificate(Certificate::from_pem(&std::fs::read(
            "/run/idle-secret/ca.crt",
        )?)?)
        .resolve(
            &hostname,
            SocketAddr::new("127.0.0.1".parse()?, address.port()),
        )
        .timeout(Duration::from_secs(2))
        .build()?;
    let status: IdleStatus = client
        .get(format!("https://{hostname}:{}/status", address.port()))
        .bearer_auth(std::fs::read_to_string(env::var("DUCKFLIGHT_IDLE_TOKEN_FILE")?)?.trim())
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    if status.protocol_version != 1 || !status.ready {
        bail!("idle-capable listeners are not ready")
    }
    Ok(())
}

#[cfg(test)]
mod tests;
