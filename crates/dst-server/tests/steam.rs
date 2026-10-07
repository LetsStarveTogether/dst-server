use dst_server::steam::{AppId, Client, PublishedFileId, ResultCode};

#[tokio::test]
#[ignore = "queries the public Steam API; run explicitly with --ignored --nocapture"]
async fn anonymous_dst_workshop_queries() -> Result<(), Box<dyn std::error::Error>> {
    let client = Client::new()?;
    let ids = [2189004162, 1803285852, 378160973, 347079953, 1840284484].map(PublishedFileId);
    let files = client.published_file_details(&ids).await?;
    assert_eq!(files.len(), ids.len());
    for (file, id) in files.into_iter().zip(ids) {
        assert_eq!(file.id, id);
        let details = file.details?;
        assert_eq!(details.consumer_app_id, AppId(322330));
        assert!(details.file_size > 0);
        assert!(details.content_handle.0 != 0 && details.content_handle.0 != u64::MAX);
        assert!(details.updated_at >= details.created_at);
        println!(
            "{}: app={} content={} bytes={} updated={}",
            id,
            details.consumer_app_id.0,
            details.content_handle.0,
            details.file_size,
            details.updated_at
        );
    }
    let mixed = client
        .published_file_details(&[ids[0], PublishedFileId(u64::MAX)])
        .await?;
    assert!(mixed[0].details.is_ok());
    assert_eq!(mixed[1].details, Err(ResultCode::FILE_NOT_FOUND));
    let repeated = client.published_file_details(&[ids[1], ids[1]]).await?;
    assert_eq!(repeated.len(), 2);
    assert!(
        repeated
            .iter()
            .all(|file| file.id == ids[1] && file.details.is_ok())
    );
    let single = client.published_file_details(&ids[..1]).await?;
    assert_eq!(single.len(), 1);
    assert_eq!(single[0].id, ids[0]);
    assert!(single[0].details.is_ok());
    Ok(())
}
