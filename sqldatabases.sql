create database foodresq;
use foodresq;
CREATE TABLE users (

    id INT AUTO_INCREMENT PRIMARY KEY,

    name VARCHAR(100),

    email VARCHAR(100) UNIQUE,

    phone VARCHAR(15),

    password VARCHAR(100)

);
CREATE TABLE ngo (

    id INT AUTO_INCREMENT PRIMARY KEY,

    ngo_name VARCHAR(100),

    email VARCHAR(100) UNIQUE,

    phone VARCHAR(15),

    location VARCHAR(100),

    password VARCHAR(100)

);

CREATE TABLE donations (

    id INT AUTO_INCREMENT PRIMARY KEY,

    user_id INT,

    food_name VARCHAR(100),

    quantity VARCHAR(50),

    food_type VARCHAR(50),

    location VARCHAR(150),

    donation_date DATE,

    donor_name VARCHAR(100),

    details TEXT,

    status VARCHAR(30) DEFAULT 'Pending',

    assigned_ngo INT DEFAULT NULL,

    donor_confirm BOOLEAN DEFAULT FALSE,

    ngo_confirm BOOLEAN DEFAULT FALSE,

    FOREIGN KEY (user_id) REFERENCES users(id),

    FOREIGN KEY (assigned_ngo) REFERENCES ngo(id)

);
CREATE TABLE food_requests (

    id INT AUTO_INCREMENT PRIMARY KEY,

    requester_name VARCHAR(100),

    phone VARCHAR(15),

    location VARCHAR(150),

    people_count INT,

    request_details TEXT,

    status VARCHAR(30) DEFAULT 'Pending'

);
CREATE TABLE admin (

    id INT AUTO_INCREMENT PRIMARY KEY,

    username VARCHAR(50),

    password VARCHAR(100)

);
INSERT INTO admin(username,password)

VALUES

('admin','admin123');
show tables;

select*from users;
drop table donations;

CREATE TABLE donations(

    donation_id INT AUTO_INCREMENT PRIMARY KEY,

    user_id INT,

    food_name VARCHAR(100),

    food_type VARCHAR(20),

    quantity INT,

    quantity_unit VARCHAR(30),

    address TEXT,

    city VARCHAR(50),

    pickup_date DATE,

    pickup_time TIME,

    contact VARCHAR(15),

    details TEXT,

    status VARCHAR(30) DEFAULT 'Pending',

    ngo_id INT DEFAULT NULL

);
DESCRIBE donations;
select * from donations;

use foodresq;
truncate table donations;

use foodresq;
ALTER TABLE donations
ADD donor_confirmation VARCHAR(10) DEFAULT 'Pending';

ALTER TABLE donations
ADD donor_feedback TEXT;

ALTER TABLE donations
ADD rating INT;

use foodresq;
CREATE TABLE ngos(

    ngo_id INT AUTO_INCREMENT PRIMARY KEY,

    ngo_name VARCHAR(100) NOT NULL,

    registration_number VARCHAR(100) NOT NULL,

    person_name VARCHAR(100) NOT NULL,

    email VARCHAR(100) UNIQUE NOT NULL,

    phone VARCHAR(20) NOT NULL,

    password VARCHAR(100) NOT NULL,

    latitude DECIMAL(10,8),

    longitude DECIMAL(11,8),

    address TEXT,

    status VARCHAR(20) DEFAULT 'Active'

);

ALTER TABLE donations
ADD COLUMN latitude DECIMAL(10,8),
ADD COLUMN longitude DECIMAL(11,8);
describe donations;

ALTER TABLE donations
MODIFY COLUMN latitude DECIMAL(10,8) AFTER address;

ALTER TABLE donations
MODIFY COLUMN longitude DECIMAL(11,8) AFTER latitude;

describe donations;
select * from donations;
select * from ngos;
ALTER TABLE ngos
ADD COLUMN partner_id VARCHAR(20);

use foodresq;
describe ngos;
select * from users;

ALTER TABLE ngos
ADD COLUMN registered_on DATE DEFAULT (CURRENT_DATE);

truncate table ngos;

ALTER TABLE ngos
ADD COLUMN service_area VARCHAR(255);
ALTER TABLE users
ADD COLUMN status VARCHAR(20) DEFAULT 'Active';

CREATE TABLE donation_reports(

report_id INT PRIMARY KEY AUTO_INCREMENT,

donation_id INT,

user_id INT,

reason VARCHAR(100),

description TEXT,

reported_on DATETIME,

status VARCHAR(30)

);

ALTER TABLE users
MODIFY password VARCHAR(255) NOT NULL;

use foodresq;
ALTER TABLE users
MODIFY password VARCHAR(255) NOT NULL;
SHOW PROCESSLIST;

KILL 769;
KILL 770;
KILL 772;
ALTER TABLE ngos
MODIFY password VARCHAR(255) NOT NULL;

select * from users;
select * from ngos;

SELECT ngo_id, email, password
FROM ngos;

ALTER TABLE users
ADD COLUMN failed_attempts INT DEFAULT 0,

ADD COLUMN locked_until DATETIME NULL;
describe users;



ALTER TABLE users
ADD COLUMN reset_otp VARCHAR(6) NULL,
ADD COLUMN reset_otp_expiry DATETIME NULL,
ADD COLUMN reset_otp_attempts INT DEFAULT 0;

show tables;

describe admin;
describe users;
describe ngos;

ALTER TABLE admin
ADD COLUMN email VARCHAR(100) UNIQUE;
UPDATE admin
SET email = 'admin@foodresq.com'
WHERE username = 'admin';
SET SQL_SAFE_UPDATES = 1;
select * from admin;

ALTER TABLE ngos
ADD COLUMN reset_otp VARCHAR(6) NULL,
ADD COLUMN reset_otp_expiry DATETIME NULL,
ADD COLUMN reset_otp_attempts INT DEFAULT 0;

ALTER TABLE ngos
ADD COLUMN failed_attempts INT DEFAULT 0,
ADD COLUMN locked_until DATETIME NULL;

ALTER TABLE admin
ADD COLUMN failed_attempts INT DEFAULT 0,
ADD COLUMN locked_until DATETIME NULL,
ADD COLUMN reset_otp VARCHAR(6) NULL,
ADD COLUMN reset_otp_expiry DATETIME NULL,
ADD COLUMN reset_otp_attempts INT DEFAULT 0;

describe admin;
SELECT id, username, email, password
FROM admin;

ALTER TABLE admin
MODIFY password VARCHAR(255);

UPDATE admin
SET password = 'scrypt:32768:8:1$PbzFNG8nxUXtg39X$31d6091348d283682431704c2b33c0b6da547baab1bd6a7c4d80a0c76108b01b57772021f69ec9249e6c2359f4b9fc055dabe53f6b7811495e6211838562d07c'
WHERE email = 'admin@foodresq.com';

truncate table users;
truncate table donations;
truncate table ngos;
truncate table donation_reports;
truncate table food_requests;

CREATE TABLE ngo_requests (
    request_id INT AUTO_INCREMENT PRIMARY KEY,
    donation_id INT NOT NULL,
    ngo_id INT NOT NULL,
    requested_on DATETIME DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(30) DEFAULT 'Pending',

    FOREIGN KEY (donation_id)
        REFERENCES donations(donation_id)
        ON DELETE CASCADE,

    FOREIGN KEY (ngo_id)
        REFERENCES ngos(ngo_id)
        ON DELETE CASCADE
);

ALTER TABLE ngos
ADD COLUMN organization_type VARCHAR(50),
ADD COLUMN year_established INT,
ADD COLUMN pincode VARCHAR(10),
ADD COLUMN darpan_id VARCHAR(100),
ADD COLUMN designation VARCHAR(100),
ADD COLUMN registration_document VARCHAR(255),
ADD COLUMN authorized_person_document VARCHAR(255),
ADD COLUMN verification_status VARCHAR(30) DEFAULT 'Pending',
ADD COLUMN rejection_reason TEXT,
ADD COLUMN verified_on DATETIME;
describe ngos;
select * from ngos;
DESCRIBE donations;
ALTER TABLE donations
ADD COLUMN food_description TEXT AFTER food_type,
ADD COLUMN serving_size VARCHAR(100) AFTER quantity_unit,
ADD COLUMN preparation_time TIME AFTER pickup_time,
ADD COLUMN expiry_time TIME AFTER preparation_time;
ALTER TABLE donations
MODIFY latitude DECIMAL(10,8) NULL,
MODIFY longitude DECIMAL(11,8) NULL;

use foodresq;
describe donations;
ALTER TABLE donations
ADD COLUMN verification_completed TINYINT(1) DEFAULT 0,
ADD COLUMN verification_date DATETIME NULL,
ADD COLUMN pickup_marked_at DATETIME NULL,
ADD COLUMN pickup_otp VARCHAR(10) NULL,
ADD COLUMN pickup_otp_expiry DATETIME NULL,
ADD COLUMN pickup_confirmed_at DATETIME NULL,
ADD COLUMN on_the_way_at DATETIME NULL,
ADD COLUMN completed_at DATETIME NULL,
ADD COLUMN last_reminder_at DATETIME NULL,
ADD COLUMN reminder_count INT DEFAULT 0;

describe donations;
SELECT 
    donation_id,
    status,
    ngo_id,
    pickup_code,
    pickup_code_verified,
    pickup_code_created_at
FROM donations
ORDER BY donation_id DESC;

describe donation_reports;
describe ngos;
ALTER TABLE donations
ADD COLUMN pickup_code VARCHAR(20) DEFAULT NULL,
ADD COLUMN pickup_code_verified TINYINT(1) DEFAULT 0,
ADD COLUMN pickup_code_created_at DATETIME DEFAULT NULL;
ALTER TABLE donation_reports
ADD COLUMN reporter_type VARCHAR(20) NOT NULL DEFAULT 'Donor';
show tables;
select * from ngo;
drop  table ngo;
DESCRIBE admin;
DESCRIBE users;
DESCRIBE ngos;
DESCRIBE donations;
DESCRIBE food_requests;
DESCRIBE ngo_requests;
DESCRIBE donation_reports;
use foodresq;
describe donations;

ALTER TABLE donations
ADD COLUMN distribution_location VARCHAR(255) NULL,
ADD COLUMN distributed_quantity INT NULL,
ADD COLUMN beneficiaries_count INT NULL,
ADD COLUMN distribution_proof VARCHAR(255) NULL,
ADD COLUMN distribution_submitted_at DATETIME NULL,
ADD COLUMN distribution_verified TINYINT(1) DEFAULT 0;

CREATE TABLE distribution_records (
    distribution_id INT AUTO_INCREMENT PRIMARY KEY,
    donation_id INT NOT NULL,
    ngo_id INT NOT NULL,

    distribution_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    recipient_type VARCHAR(100),
    people_served INT DEFAULT 0,

    distribution_location VARCHAR(255),
    quantity_distributed INT DEFAULT 0,
    quantity_unit VARCHAR(50),

    notes TEXT,

    proof_method VARCHAR(100) DEFAULT 'NGO Confirmation',

    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (donation_id)
        REFERENCES donations(donation_id)
        ON DELETE CASCADE
);
CREATE TABLE notifications (
    notification_id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NULL,
    ngo_id INT NULL,
    donation_id INT NULL,
    message TEXT NOT NULL,
    is_read TINYINT(1) DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE donations
ADD COLUMN landmark VARCHAR(255) NULL AFTER address;
describe donations;
ALTER TABLE donations
ADD COLUMN prepared_date DATE NULL AFTER preparation_time,
ADD COLUMN storage_condition VARCHAR(50) NULL AFTER expiry_time,
ADD COLUMN allergen_status VARCHAR(50) NULL AFTER storage_condition,
ADD COLUMN allergens TEXT NULL AFTER allergen_status,
ADD COLUMN pickup_instructions TEXT NULL AFTER contact,
ADD COLUMN packaging_type VARCHAR(50) NULL AFTER pickup_instructions;
ALTER TABLE donations
ADD COLUMN expiry_date DATE NULL AFTER preparation_time;
SELECT
    donation_id,
    food_name,
    address,
    landmark,
    city,
    latitude,
    longitude,
    prepared_date,
    preparation_time,
    expiry_date,
    expiry_time,
    storage_condition,
    allergen_status,
    allergens,
    pickup_date,
    pickup_time,
    contact,
    pickup_instructions,
    packaging_type,
    details
FROM donations
ORDER BY donation_id DESC
LIMIT 1;
ALTER TABLE users
ADD COLUMN deactivated_until DATETIME NULL;

-- ============================================================
-- Access queue (limits how many people can be logged in at once)
-- ============================================================
USE foodresq;
CREATE TABLE IF NOT EXISTS access_queue (
    token       CHAR(43)     NOT NULL PRIMARY KEY,
    role        VARCHAR(10)  NOT NULL,
    status      VARCHAR(10)  NOT NULL,
    created_at  DATETIME(3)  NOT NULL,
    last_seen   DATETIME(3)  NOT NULL,
    INDEX idx_status_created (status, created_at),
    INDEX idx_last_seen (last_seen)
) ENGINE=InnoDB;
